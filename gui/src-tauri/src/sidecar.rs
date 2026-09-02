//! Spawns and supervises the Python API process.
//!
//! Three things here are load-bearing:
//!
//! 1. **Finding the binary.** Apps launched from Finder or Spotlight do not
//!    inherit the login shell's PATH, so `~/.local/bin/vikram-api` — where
//!    `uv tool install` puts it — is invisible. `vikram gui` passes an absolute
//!    path in `VIKRAM_API_BIN`; failing that we probe the usual locations and,
//!    failing that, say so plainly instead of spinning forever.
//! 2. **The token goes in the environment, never argv.** argv is world-readable
//!    through `ps`.
//! 3. **Reading the port from stdout.** The server binds its own socket and
//!    prints one JSON line, so there is no poll loop and no bind race.

use std::path::PathBuf;
use std::sync::Mutex;

use rand::RngCore;
use serde::Serialize;
use tauri::{AppHandle, Manager};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::oneshot;

/// Where `uv tool install`, Homebrew and cargo put binaries. Probed in order
/// because a Finder launch gives us almost no PATH to work with.
const PROBE_DIRS: &[&str] = &[
    ".local/bin",
    ".cargo/bin",
];
const SYSTEM_PROBE_DIRS: &[&str] = &[
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
];

#[derive(Clone, Serialize)]
pub struct ApiConfig {
    pub base_url: String,
    pub token: String,
}

#[derive(Default)]
pub struct SidecarState {
    pub config: Mutex<Option<ApiConfig>>,
    pub child: Mutex<Option<CommandChild>>,
}

#[derive(Debug, Serialize)]
pub struct SidecarError {
    pub kind: String,
    pub message: String,
    pub hint: Option<String>,
}

impl SidecarError {
    fn not_found() -> Self {
        Self {
            kind: "binary_not_found".into(),
            message: "Could not find the `vikram-api` executable.".into(),
            hint: Some(
                "Install it with `uv tool install --from . vikram`, then launch \
                 this app with `vikram gui` so it can pass the path through."
                    .into(),
            ),
        }
    }
}

fn generate_token() -> String {
    let mut bytes = [0u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Locate `vikram-api` without relying on an inherited PATH.
pub fn resolve_api_bin() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("VIKRAM_API_BIN") {
        let path = PathBuf::from(explicit);
        if path.is_file() {
            return Some(path);
        }
    }
    if let Some(home) = dirs_home() {
        for dir in PROBE_DIRS {
            let candidate = home.join(dir).join("vikram-api");
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    for dir in SYSTEM_PROBE_DIRS {
        let candidate = PathBuf::from(dir).join("vikram-api");
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

fn dirs_home() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

/// Start the API and wait for its readiness line.
pub async fn spawn(app: &AppHandle) -> Result<ApiConfig, SidecarError> {
    let binary = resolve_api_bin().ok_or_else(SidecarError::not_found)?;
    let token = generate_token();

    let command = app
        .shell()
        .command(binary.to_string_lossy().to_string())
        .args(["--gui", "--host", "127.0.0.1", "--port", "0"])
        // Environment, not argv: `ps` would expose an argv token to any user.
        .env("VIKRAM_GUI_TOKEN", token.clone())
        .env("VIKRAM_GUI_ENABLED", "1")
        // If this window dies, the API must not keep listening.
        .env("VIKRAM_GUI_PARENT_PID", std::process::id().to_string());

    let (mut rx, child) = command.spawn().map_err(|err| SidecarError {
        kind: "spawn_failed".into(),
        message: format!("Could not start {}: {err}", binary.display()),
        hint: None,
    })?;

    let (ready_tx, ready_rx) = oneshot::channel::<Result<u16, String>>();
    let mut ready_tx = Some(ready_tx);

    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let text = String::from_utf8_lossy(&line);
                    // stdout carries only the handshake; logs go to stderr.
                    if let Some(port) = parse_ready(&text) {
                        if let Some(tx) = ready_tx.take() {
                            let _ = tx.send(Ok(port));
                        }
                    }
                }
                CommandEvent::Stderr(line) => {
                    eprintln!("[vikram-api] {}", String::from_utf8_lossy(&line).trim_end());
                }
                CommandEvent::Terminated(payload) => {
                    if let Some(tx) = ready_tx.take() {
                        let _ = tx.send(Err(format!(
                            "vikram-api exited before it was ready (code {:?}).",
                            payload.code
                        )));
                    }
                    break;
                }
                _ => {}
            }
        }
    });

    let port = match tokio::time::timeout(std::time::Duration::from_secs(60), ready_rx).await {
        Ok(Ok(Ok(port))) => port,
        Ok(Ok(Err(message))) => {
            return Err(SidecarError {
                kind: "exited".into(),
                message,
                hint: Some("Run `vikram doctor` to check the configuration.".into()),
            })
        }
        _ => {
            return Err(SidecarError {
                kind: "timeout".into(),
                message: "vikram-api did not report a port within 60 seconds.".into(),
                hint: None,
            })
        }
    };

    let config = ApiConfig {
        base_url: format!("http://127.0.0.1:{port}"),
        token,
    };
    let state = app.state::<SidecarState>();
    *state.config.lock().unwrap() = Some(config.clone());
    *state.child.lock().unwrap() = Some(child);
    Ok(config)
}

fn parse_ready(line: &str) -> Option<u16> {
    let value: serde_json::Value = serde_json::from_str(line.trim()).ok()?;
    if value.get("vikram_api_ready")?.as_bool()? {
        return value.get("port")?.as_u64().map(|port| port as u16);
    }
    None
}

/// Terminate the sidecar. Called on window close and app exit.
pub fn shutdown(app: &AppHandle) {
    let state = app.state::<SidecarState>();
    // Take the child out under its own scope so the guard drops before
    // `state` does.
    let child = { state.child.lock().unwrap().take() };
    if let Some(child) = child {
        let _ = child.kill();
    }
}
