#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod bridge;
mod browser;
use base64::Engine;
use bridge::Bridge;
use browser::{Bounds, Browser};
use serde_json::{json, Value};
use std::{io::Read, path::PathBuf, sync::atomic::Ordering};
use tauri::{AppHandle, Emitter, Manager, State};

const METHODS: &[&str] = &[
    "hello",
    "snapshot",
    "refresh",
    "stop",
    "chat",
    "create_chat",
    "update_chat",
    "draft",
    "preference",
    "create_folder",
    "rename_folder",
    "delete_folder",
    "delete_chat",
    "import_image",
    "import_attachment",
    "attachment",
    "remove_attachment",
    "image",
    "remove_image",
    "send",
    "respond",
    "schedules",
    "schedule",
    "schedule_run",
    "create_schedule",
    "update_schedule",
    "delete_schedule",
    "run_schedule",
    "open_run",
];

#[tauri::command]
async fn rpc(
    app: AppHandle,
    state: State<'_, Bridge>,
    method: String,
    params: Value,
) -> Result<Value, String> {
    if !METHODS.contains(&method.as_str()) {
        return Err("Unknown command".into());
    }
    state.call(&app, &method, params).await
}

#[tauri::command]
async fn reconnect(app: AppHandle) -> Result<Value, String> {
    let handle = app.clone();
    tauri::async_runtime::spawn_blocking(move || handle.state::<Bridge>().shutdown())
        .await
        .map_err(|_| "Service shutdown failed")?;
    app.state::<Bridge>().call(&app, "hello", json!({})).await
}

async fn import_files(
    app: &AppHandle,
    chat_id: &str,
    paths: Vec<PathBuf>,
) -> Result<Value, String> {
    let mut imported = Vec::new();
    let mut errors = Vec::new();
    for path in paths.iter().take(4) {
        let result = (|| {
            let mut file =
                std::fs::File::open(path).map_err(|_| "The selected file cannot be read.")?;
            if !file
                .metadata()
                .map_err(|_| "Cannot read the selected file.")?
                .is_file()
            {
                return Err("Choose an regular file.");
            }
            let mut bytes = Vec::new();
            (&mut file)
                .take(10 * 1024 * 1024 + 1)
                .read_to_end(&mut bytes)
                .map_err(|_| "Cannot read the selected file.")?;
            if bytes.len() > 10 * 1024 * 1024 {
                return Err("Files must be no larger than 10 MiB.");
            }
            Ok(bytes)
        })();
        match result {
            Ok(bytes) => {
                let name = path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("attachment");
                match app.state::<Bridge>().call(app, "import_attachment", json!({"chat_id":chat_id,"name":name,"data":base64::engine::general_purpose::STANDARD.encode(bytes)})).await {
                    Ok(value) => imported.push(value), Err(error) => errors.push(error),
                }
            }
            Err(error) => errors.push(error.to_string()),
        }
    }
    if paths.len() > 4 {
        errors.push("Attach up to four files per message.".into());
    }
    Ok(json!({"imported":imported,"errors":errors}))
}

#[tauri::command]
async fn pick_attachments(app: AppHandle, chat_id: String) -> Result<Value, String> {
    let paths = tauri::async_runtime::spawn_blocking(|| {
        rfd::FileDialog::new()
            .set_title("Attach files")
            .add_filter(
                "Images and documents",
                &[
                    "png", "jpg", "jpeg", "webp", "pdf", "docx", "xlsx", "csv", "tsv", "txt", "md",
                    "json", "yaml", "yml", "toml", "py", "rs", "js", "ts", "tsx", "html", "css",
                ],
            )
            .add_filter("Text and code files", &["*"])
            .pick_files()
    })
    .await
    .map_err(|_| "The file picker could not open.")?;
    import_files(&app, &chat_id, paths.unwrap_or_default()).await
}

#[tauri::command]
async fn import_drop(app: AppHandle, chat_id: String) -> Result<Value, String> {
    let paths = std::mem::take(&mut *app.state::<Bridge>().drops.lock().unwrap());
    import_files(&app, &chat_id, paths).await
}

#[tauri::command]
fn copy_text(text: String) -> Result<(), String> {
    arboard::Clipboard::new()
        .and_then(|mut c| c.set_text(text))
        .map_err(|_| "Could not copy. Select the text and press Command-C.".into())
}

#[tauri::command]
fn open_link(url: String) -> Result<(), String> {
    let parsed = url::Url::parse(&url).map_err(|_| "Invalid link")?;
    if !["https", "http"].contains(&parsed.scheme())
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return Err("Only web links may be opened.".into());
    }
    open::that(parsed.as_str()).map_err(|_| "Could not open the browser.".into())
}

/// Where the window shows the browser panel, in its page's coordinates.
#[tauri::command]
async fn browser_bounds(app: AppHandle, x: f64, y: f64, width: f64, height: f64) {
    if [x, y, width, height].iter().all(|v| v.is_finite()) && width >= 1.0 && height >= 1.0 {
        app.state::<Browser>()
            .set_bounds(&app, Bounds { x, y, width, height })
            .await;
    }
}

/// Hide the panel while the window shows something over it, such as a dialog.
#[tauri::command]
fn browser_visible(browser: State<'_, Browser>, visible: bool) {
    browser.set_hidden(!visible);
}

#[tauri::command]
fn browser_state(browser: State<'_, Browser>) -> Value {
    browser.snapshot()
}

/// The person closed the browser: its private session ends, and the reply that
/// was using it is told.
#[tauri::command]
async fn browser_close(app: AppHandle) -> Result<(), String> {
    app.state::<Browser>().close(&app);
    app.state::<Bridge>()
        .call(&app, "browser_closed", json!({}))
        .await
        .map(|_| ())
}

#[tauri::command]
async fn quit(app: AppHandle) {
    app.state::<Bridge>().exiting.store(true, Ordering::SeqCst);
    let handle = app.clone();
    let _ = tauri::async_runtime::spawn_blocking(move || handle.state::<Bridge>().shutdown()).await;
    app.exit(0);
}

fn main() {
    let context = tauri::generate_context!();
    if std::env::args().nth(1).as_deref() == Some("--browser-helper") {
        // The CLI's hidden browser: no window, Dock icon, or chat service.
        browser::helper(context);
        return;
    }
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .manage(Bridge::default())
        .manage(Browser::default())
        .invoke_handler(tauri::generate_handler![
            rpc,
            reconnect,
            pick_attachments,
            import_drop,
            copy_text,
            open_link,
            browser_bounds,
            browser_visible,
            browser_state,
            browser_close,
            quit
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::DragDrop(tauri::DragDropEvent::Drop { paths, .. }) = event {
                *window.state::<Bridge>().drops.lock().unwrap() = paths.clone();
                let _ = window.emit("files-dropped", ());
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.emit("quit-requested", ());
            }
        })
        .build(context)
        .expect("Could not start Vikram");
    app.run(|app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            let state = app.state::<Bridge>();
            if !state.exiting.load(Ordering::SeqCst) {
                api.prevent_exit();
                let _ = app.emit("quit-requested", ());
            }
        }
    });
}
