// PRISM Tauri shell (v6.0.0 dev) — spawn the local Python service as a
// child process on app boot, point the WebView at it, kill the child on
// app exit. For installed bundles (.msi/.dmg/.AppImage), this will be
// replaced with a Tauri sidecar binary in v6.0.1.

use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

struct ServiceChild(Mutex<Option<Child>>);

fn spawn_prism_service() -> std::io::Result<Child> {
    // Where is the service code? PRISM_SERVICE_DIR overrides; otherwise
    // default to the worktree path for the v6.0.0-dev shell. For an
    // installed bundle, Tauri's sidecar mechanism replaces this whole
    // function (v6.0.1+ packaging).
    let cwd = std::env::var("PRISM_SERVICE_DIR").unwrap_or_else(|_| {
        r"E:\.prism\.claude\worktrees\v6-pivot\services\prism-service".to_string()
    });

    // Use the 8.3 short path so Python's own argv parser can't split it
    // on the space inside "Program Files". py.exe + the full path both
    // result in `C:\Program Files\...` reaching python.exe as a single
    // unquoted argv[0] which the msvcrt parser then truncates at the
    // first space — Python opens "Files\Python312\python.exe" as a script
    // (relative to cwd) and crashes ENOENT.
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
            "17778",
            "--mcp-port",
            "17777",
        ])
        .current_dir(&cwd)
        .env("PRISM_DATA_DIR", r"C:\Users\siege\.claude\jobs\eeadb7d2\v6-data")
        .spawn()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .manage(ServiceChild(Mutex::new(None)))
        .setup(|app| {
            match spawn_prism_service() {
                Ok(child) => {
                    let pid = child.id();
                    eprintln!("[prism-shell] service started (pid {pid})");
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
