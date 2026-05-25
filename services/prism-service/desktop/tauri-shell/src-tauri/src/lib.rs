// PRISM Tauri shell (v6.0.0 dev) — spawns the local Python service on
// boot, points the WebView at it, and uses a Windows Job Object so the
// child cascade-dies with us (RunEvent::Exit alone wasn't reliable on
// abrupt parent termination; Job Objects are OS-level).

use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

#[cfg(windows)]
mod jobkill {
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use windows_sys::Win32::Foundation::HANDLE;
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
        JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    /// Put `child` into a Job Object with KILL_ON_JOB_CLOSE so the OS
    /// kills the child the moment our Job HANDLE closes — even on
    /// `taskkill /F` against prism-shell.exe.
    ///
    /// Returns the Job HANDLE; the caller stashes it (a closed handle
    /// is what triggers the cascade kill).
    pub fn assign_kill_on_close(child: &Child) -> Result<HANDLE, String> {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return Err("CreateJobObjectW returned NULL".into());
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if ok == 0 {
                return Err("SetInformationJobObject failed".into());
            }
            let child_handle = child.as_raw_handle() as HANDLE;
            let ok = AssignProcessToJobObject(job, child_handle);
            if ok == 0 {
                return Err("AssignProcessToJobObject failed".into());
            }
            Ok(job)
        }
    }
}

struct ServiceChild(Mutex<Option<Child>>);

#[cfg(windows)]
struct JobHandle(Mutex<Option<windows_sys::Win32::Foundation::HANDLE>>);
#[cfg(windows)]
unsafe impl Send for JobHandle {}
#[cfg(windows)]
unsafe impl Sync for JobHandle {}

/// Resolve the directory containing prism_service/pyproject.toml.
/// Three lookup layers in order:
///   1. `PRISM_SERVICE_DIR` env var override.
///   2. Walk up from the running exe — covers installed bundles where
///      Tauri drops the python tree next to the binary, plus most
///      pip-install layouts where the wheel lives next to its source.
///   3. Compile-time `CARGO_MANIFEST_DIR` 3 levels up — covers
///      `cargo tauri dev` where the exe is in `tauri-target/` (often on
///      a different drive from the worktree) but the source still lives
///      where it was at compile time. Falls through gracefully if that
///      path doesn't exist on the user's box (e.g. a CI-built bundle
///      where the manifest_dir referenced the runner's checkout).
fn find_service_dir() -> std::path::PathBuf {
    use std::path::PathBuf;

    if let Ok(d) = std::env::var("PRISM_SERVICE_DIR") {
        return PathBuf::from(d);
    }
    if let Ok(exe) = std::env::current_exe() {
        for parent in exe.ancestors() {
            let candidate = parent.join("services").join("prism-service");
            if candidate.join("pyproject.toml").is_file() {
                return candidate;
            }
            if parent.join("pyproject.toml").is_file()
                && parent.join("prism_service").is_dir()
            {
                return parent.to_path_buf();
            }
        }
    }
    // src-tauri/../../.. = services/prism-service (the service dir).
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    if let Some(dev) = manifest_dir
        .parent()                  // tauri-shell
        .and_then(|p| p.parent())  // desktop
        .and_then(|p| p.parent())  // prism-service
    {
        if dev.join("pyproject.toml").is_file() {
            return dev.to_path_buf();
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

/// Pick the Python invocation to spawn the service with. Honors
/// `PRISM_PYTHON` first; on Windows tries the 8.3 short path of
/// `C:\Program Files\Python312\python.exe` to dodge the argv-split bug
/// when the canonical path has spaces; on Linux/macOS defaults to
/// `python3`. Returns a Vec so we can use multi-arg launchers like
/// `py -3` if needed later.
fn find_python() -> Vec<String> {
    if let Ok(p) = std::env::var("PRISM_PYTHON") {
        return p.split_whitespace().map(String::from).collect();
    }
    if cfg!(windows) {
        // 8.3 short name works when the canonical install path is
        // `C:\Program Files\Python312\python.exe`. If the user has
        // python in a different location, they can override via
        // PRISM_PYTHON. Falling back to bare `python` for non-standard
        // installs (will resolve via PATH).
        let short = r"C:\PROGRA~1\PYTHON~1\python.exe";
        if std::path::Path::new(short).exists() {
            return vec![short.to_string()];
        }
        return vec!["python".to_string()];
    }
    // POSIX — PEP 394 makes python3 canonical and python.org installs
    // ship it as /usr/bin/python3. Override via PRISM_PYTHON if a venv
    // is required.
    vec!["python3".to_string()]
}

fn spawn_prism_service() -> std::io::Result<Child> {
    let cwd = find_service_dir();
    let python = find_python();
    let python_exe = python.first().cloned().unwrap_or_else(|| "python3".to_string());

    // v5.3.21 — tell the spawned service whether we're a dev or release
    // shell. The shell's own compile-time version is "0.1.0" only on
    // local cargo tauri dev; installed bundles have the real
    // PRISM_VERSION baked in via the v5.3.19 CI sync step. SPA footer
    // surfaces this so the user can tell at a glance which build is live.
    let shell_version = env!("CARGO_PKG_VERSION");
    let build_mode = if shell_version.starts_with("0.0") || shell_version == "0.1.0" {
        "dev"
    } else {
        "release"
    };

    eprintln!("[prism-shell] python: {python:?}");
    eprintln!("[prism-shell] cwd:    {}", cwd.display());
    eprintln!("[prism-shell] build_mode: {build_mode} (shell version {shell_version})");

    let mut cmd = Command::new(&python_exe);
    // Trailing python args (e.g. `-3` for the py launcher) before the
    // module/arguments.
    for arg in python.iter().skip(1) {
        cmd.arg(arg);
    }
    cmd.args([
        "-m",
        "prism_service.cli.prism_cli",
        "start",
        "--ui-port",
        "7778",
        "--mcp-port",
        "7777",
    ])
    .current_dir(&cwd)
    .env("PRISM_BUILD_MODE", build_mode)
    .env("PRISM_SHELL_VERSION", shell_version);
    // Note: we intentionally do NOT set PRISM_DATA_DIR here. The python
    // service's resolve_data_dir() handles platform-aware defaults
    // (%LOCALAPPDATA%\prism on Windows, ~/.prism on Linux/macOS, /data
    // in docker). Users who want a non-default data dir can set
    // PRISM_DATA_DIR in their environment before launching the shell;
    // it's inherited automatically.
    cmd.spawn()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(ServiceChild(Mutex::new(None)));

    #[cfg(windows)]
    let app = app.manage(JobHandle(Mutex::new(None)));

    let app = app
        .setup(|app| {
            // v5.3.20 — auto-update only for production builds. Dev
            // binaries have tauri.conf.json version "0.1.0" (the scaffold
            // default) and never get the CI sync step that bumps them to
            // PRISM_VERSION. Without this guard, every dev launch sees
            // "update available: 0.1.0 -> 5.3.X", downloads the bundle,
            // tries to install — but the dev binary doesn't get replaced
            // (it lives at target/debug/, not in Program Files), so the
            // next launch fires the same loop. Annoying + spends user
            // bandwidth on a no-op. The installed .msi carries the real
            // version and exits this guard, so it auto-updates as intended.
            let pkg_version = app.package_info().version.to_string();
            if pkg_version.starts_with("0.0") || pkg_version == "0.1.0" {
                eprintln!(
                    "[prism-shell] dev build (v{pkg_version}) — skipping updater check"
                );
            } else {
                let handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    use tauri_plugin_updater::UpdaterExt;
                    match handle.updater() {
                        Ok(updater) => match updater.check().await {
                            Ok(Some(update)) => {
                                eprintln!(
                                    "[prism-shell] update available: {} -> {}",
                                    update.current_version, update.version,
                                );
                                let _ = update
                                    .download_and_install(|_, _| {}, || {
                                        eprintln!("[prism-shell] update installed; restart pending");
                                    })
                                    .await;
                            }
                            Ok(None) => eprintln!("[prism-shell] no update available"),
                            Err(e) => eprintln!("[prism-shell] update check failed: {e}"),
                        },
                        Err(e) => eprintln!("[prism-shell] updater unavailable: {e}"),
                    }
                });
            }

            match spawn_prism_service() {
                Ok(child) => {
                    let pid = child.id();
                    eprintln!("[prism-shell] service started (pid {pid})");
                    #[cfg(windows)]
                    {
                        match jobkill::assign_kill_on_close(&child) {
                            Ok(job) => {
                                *app.state::<JobHandle>().0.lock().unwrap() = Some(job);
                                eprintln!("[prism-shell] child assigned to kill-on-close job object");
                            }
                            Err(e) => {
                                eprintln!("[prism-shell] failed to assign job object: {e}");
                            }
                        }
                    }
                    *app.state::<ServiceChild>().0.lock().unwrap() = Some(child);
                }
                Err(e) => {
                    eprintln!("[prism-shell] failed to spawn service: {e}");
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            // Best-effort SIGTERM equivalent — the Job Object kills the
            // child even if this misses, but a clean kill avoids the
            // "process didn't shut down cleanly" log noise.
            if let Some(mut child) = app_handle
                .state::<ServiceChild>()
                .0
                .lock()
                .unwrap()
                .take()
            {
                eprintln!("[prism-shell] killing service (pid {})", child.id());
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    });
}
