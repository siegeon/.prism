// PRISM Tauri shell — spawns the local Python service on boot, points
// the WebView at it, and uses a Windows Job Object so the child
// cascade-dies with us. The bundled `index.html` splash polls the
// backend and navigates to http://127.0.0.1:7778/ once it's healthy
// (issue #84 — previously the window opened directly on the URL and
// rendered ERR_CONNECTION_REFUSED on fresh installs without a backend).

use std::path::PathBuf;
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

/// Resolve the prism-service launcher in priority order:
///
/// 1. `PRISM_PYTHON` + `PRISM_SERVICE_DIR` env vars — developer override
///    used by the editable-install + source-run workflow.
/// 2. `prism` on PATH — pipx-installed users get this for free
///    (`pipx install prism-service` puts `prism.exe` on PATH).
/// 3. Bundled sidecar `prism-service.exe` next to `prism-shell.exe` —
///    Phase 2 deliverable; not present today but the discovery slot is
///    here so a frozen build drops in without another lib.rs change.
///
/// Returns `None` if no launcher is found — the splash will time out
/// and show the "Backend failed to start" UI.
fn resolve_launcher() -> Option<Command> {
    let ui_port = "7778";
    let mcp_port = "7777";

    // (1) dev override
    if let (Ok(python), Ok(svc_dir)) = (
        std::env::var("PRISM_PYTHON"),
        std::env::var("PRISM_SERVICE_DIR"),
    ) {
        eprintln!("[prism-shell] launcher: dev override (python={python}, dir={svc_dir})");
        let mut cmd = Command::new(python);
        cmd.args([
            "-m",
            "prism_service.cli.prism_cli",
            "start",
            "--ui-port",
            ui_port,
            "--mcp-port",
            mcp_port,
        ])
        .current_dir(svc_dir);
        return Some(cmd);
    }

    // (2) `prism` on PATH (pipx-installed)
    if which("prism").is_some() {
        eprintln!("[prism-shell] launcher: `prism` on PATH");
        let mut cmd = Command::new("prism");
        cmd.args([
            "start",
            "--foreground",
            "--ui-port",
            ui_port,
            "--mcp-port",
            mcp_port,
        ]);
        return Some(cmd);
    }

    // (3) bundled sidecar next to prism-shell.exe (Phase 2 — not yet shipped)
    if let Some(sidecar) = sidecar_path() {
        if sidecar.exists() {
            eprintln!("[prism-shell] launcher: bundled sidecar ({})", sidecar.display());
            let mut cmd = Command::new(&sidecar);
            cmd.args([
                "start",
                "--foreground",
                "--ui-port",
                ui_port,
                "--mcp-port",
                mcp_port,
            ]);
            return Some(cmd);
        }
    }

    eprintln!("[prism-shell] no launcher found — splash will show install instructions");
    None
}

/// Return the path where a bundled `prism-service` sidecar would live.
/// We don't ship one yet (Phase 2), but the discovery code probes here.
fn sidecar_path() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let dir = exe.parent()?;
    #[cfg(windows)]
    let name = "prism-service.exe";
    #[cfg(not(windows))]
    let name = "prism-service";
    Some(dir.join(name))
}

/// Minimal `which` — checks $PATH entries for an executable. Avoids
/// pulling the `which` crate just for this one call.
fn which(name: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    #[cfg(windows)]
    let exts: Vec<String> = std::env::var("PATHEXT")
        .unwrap_or_else(|_| ".EXE;.CMD;.BAT;.COM".into())
        .split(';')
        .map(|s| s.to_string())
        .collect();
    #[cfg(not(windows))]
    let exts: Vec<String> = vec!["".into()];

    for dir in std::env::split_paths(&path_var) {
        for ext in &exts {
            let candidate = dir.join(format!("{name}{ext}"));
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
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
            // Updater check (unchanged from prior versions).
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

            // Backend spawn with ordered discovery. If no launcher is
            // found, we DON'T error out — the splash will time out and
            // show the "Backend failed to start" UI with install docs.
            // This lets us still surface a UI on a backend-less install.
            match resolve_launcher() {
                Some(mut cmd) => match cmd.spawn() {
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
                },
                None => {
                    eprintln!(
                        "[prism-shell] no prism-service launcher found on this system; \
                         splash will offer install instructions"
                    );
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
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
