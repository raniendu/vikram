#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Vikram Studio — a thin native shell around the Python runtime.
//!
//! The shell owns three things: starting `vikram-api` as a sidecar, handing the
//! webview its base URL and bearer token, and making sure the API dies when the
//! window does. Everything else is the web frontend talking to that API.

mod sidecar;

use sidecar::{ApiConfig, SidecarError, SidecarState};
use tauri::{Manager, RunEvent, WindowEvent};

/// The frontend asks for this once on mount. Starting the sidecar lazily here
/// (rather than in `setup`) keeps the window paintable while Python boots, so a
/// slow start shows a status screen instead of a blank rectangle.
#[tauri::command]
async fn api_config(app: tauri::AppHandle) -> Result<ApiConfig, SidecarError> {
    if let Some(existing) = app.state::<SidecarState>().config.lock().unwrap().clone() {
        return Ok(existing);
    }
    sidecar::spawn(&app).await
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(SidecarState::default())
        .invoke_handler(tauri::generate_handler![api_config])
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::Destroyed) {
                sidecar::shutdown(window.app_handle());
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build Vikram Studio")
        .run(|app, event| {
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                sidecar::shutdown(app);
            }
        });
}
