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

fn spawn_prism_service() -> std::io::Result<Child> {
    let cwd = std::env::var("PRISM_SERVICE_DIR").unwrap_or_else(|_| {
        r"E:\.prism\.claude\worktrees\v6-pivot\services\prism-service".to_string()
    });
    // 8.3 short path so msvcrt's argv parser doesn't choke on the space
    // in "C:\Program Files\".
    let python = std::env::var("PRISM_PYTHON")
        .unwrap_or_else(|_| r"C:\PROGRA~1\PYTHON~1\python.exe".to_string());

    eprintln!("[prism-shell] python: {python}");
    eprintln!("[prism-shell] cwd:    {cwd}");

    Command::new(&python)
        .args([
            "-m",
            "prism_service.cli.prism_cli",
            "start",
            "--ui-port",
            "7778",
            "--mcp-port",
            "7777",
        ])
        .current_dir(&cwd)
        .env("PRISM_DATA_DIR", r"C:\Users\siege\.claude\jobs\eeadb7d2\v6-data")
        .spawn()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(ServiceChild(Mutex::new(None)));

    #[cfg(windows)]
    let app = app.manage(JobHandle(Mutex::new(None)));

    let app = app
        .setup(|app| {
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
