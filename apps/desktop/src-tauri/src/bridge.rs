use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Read, Write},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::{sync_channel, SyncSender},
        Mutex,
    },
    time::Duration,
};
use tauri::{AppHandle, Emitter, Manager};
use tokio::sync::oneshot;

struct Process {
    child: Child,
    input: SyncSender<Vec<u8>>,
    generation: u64,
}
struct Pending {
    generation: u64,
    reply: oneshot::Sender<Result<Value, String>>,
}

fn terminate(process: Process) {
    let Process {
        mut child, input, ..
    } = process;
    drop(input); // The dedicated writer closes stdin after draining accepted commands.
    for _ in 0..40 {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = child.kill(); // Independent of the writer: blocked writes cannot hold this lock.
    let _ = child.wait();
}

fn writer(child: &mut Child) -> Result<SyncSender<Vec<u8>>, String> {
    let mut input = child.stdin.take().ok_or("Service input unavailable")?;
    let (tx, rx) = sync_channel::<Vec<u8>>(8);
    std::thread::spawn(move || {
        while let Ok(bytes) = rx.recv() {
            if input.write_all(&bytes).is_err() {
                break;
            }
        }
    });
    Ok(tx)
}

#[derive(Default)]
pub struct Bridge {
    process: Mutex<Option<Process>>,
    pending: Mutex<HashMap<String, Pending>>,
    serial: AtomicU64,
    generation: AtomicU64,
    pub exiting: AtomicBool,
    pub drops: Mutex<Vec<std::path::PathBuf>>,
}

impl Bridge {
    fn launch(&self, app: &AppHandle) -> Result<(), String> {
        let mut guard = self.process.lock().map_err(|_| "Service unavailable")?;
        if guard.is_some() {
            return Ok(());
        }
        let generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        let mut command;
        if cfg!(debug_assertions) {
            let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
            command = Command::new(repo.join(".venv/bin/python"));
            command.current_dir(repo).args(["-m", "vikram.desktop"]);
        } else {
            let exe = std::env::current_exe().map_err(|_| "Cannot locate bundled service")?;
            command = Command::new(
                exe.parent()
                    .ok_or("Cannot locate bundled service")?
                    .join("vikram-sidecar"),
            );
        }
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "Cannot start the bundled chat service. Reconnect or rebuild Vikram.")?;
        let input = writer(&mut child)?;
        let output = child.stdout.take().ok_or("Service output unavailable")?;
        *guard = Some(Process {
            child,
            input,
            generation,
        });
        let handle = app.clone();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(output);
            loop {
                let mut line = String::new();
                match reader.by_ref().take(64 * 1024 * 1024).read_line(&mut line) {
                    Ok(0) | Err(_) => break,
                    Ok(_) if !line.ends_with('\n') => break,
                    _ => {}
                }
                let Ok(value) = serde_json::from_str::<Value>(&line) else {
                    break;
                };
                let bridge = handle.state::<Bridge>();
                if bridge.generation.load(Ordering::SeqCst) != generation
                    || value.get("v") != Some(&json!(1))
                {
                    break;
                }
                if let Some(id) = value.get("id").and_then(Value::as_str) {
                    if let Some(pending) = bridge.pending.lock().unwrap().remove(id) {
                        let result = match value.get("error").and_then(Value::as_str) {
                            Some(error) => Err(error.to_string()),
                            None => Ok(value.get("result").cloned().unwrap_or(Value::Null)),
                        };
                        let _ = pending.reply.send(result);
                    }
                } else if value.get("event").and_then(Value::as_str) == Some("browser") {
                    // A browser action for the panel; never shown to the window.
                    run_browser_action(handle.clone(), value);
                } else {
                    let _ = handle.emit("runtime", value);
                }
            }
            let bridge = handle.state::<Bridge>();
            if bridge.shutdown_generation(Some(generation)) {
                let _ = handle.emit("runtime", json!({"v":1,"event":"disconnected"}));
            }
        });
        Ok(())
    }

    pub async fn call(
        &self,
        app: &AppHandle,
        method: &str,
        params: Value,
    ) -> Result<Value, String> {
        // Only an explicit handshake/reconnect may start a new service generation.
        if method == "hello" {
            self.launch(app)?;
        }
        let id = format!("r{}", self.serial.fetch_add(1, Ordering::SeqCst));
        let line = serde_json::to_string(&json!({"v":1,"id":id,"method":method,"params":params}))
            .map_err(|_| "Invalid command")?
            + "\n";
        if line.len() > 15 * 1024 * 1024 {
            return Err("Image or command exceeds the size limit.".into());
        }
        let (tx, rx) = oneshot::channel();
        let generation = {
            let guard = self.process.lock().unwrap();
            let process = guard
                .as_ref()
                .ok_or("The chat service is disconnected. Choose Reconnect.")?;
            self.pending.lock().unwrap().insert(
                id.clone(),
                Pending {
                    generation: process.generation,
                    reply: tx,
                },
            );
            if process.input.try_send(line.into_bytes()).is_err() {
                self.pending.lock().unwrap().remove(&id);
                return Err(
                    "The chat service is busy or disconnected. Try again or reconnect.".into(),
                );
            }
            process.generation
        };
        // Includes queueing and pipe writes, which happen off the async runtime.
        match tokio::time::timeout(Duration::from_secs(120), rx).await {
            Ok(Ok(result)) => result,
            _ => {
                let handle = app.clone();
                let _ = tauri::async_runtime::spawn_blocking(move || {
                    if handle
                        .state::<Bridge>()
                        .shutdown_generation(Some(generation))
                    {
                        let _ = handle.emit("runtime", json!({"v":1,"event":"disconnected"}));
                    }
                })
                .await;
                Err("The chat service stopped responding and was closed. Reconnect to recover saved work.".into())
            }
        }
    }

    fn shutdown_generation(&self, expected: Option<u64>) -> bool {
        let mut guard = self.process.lock().unwrap();
        let Some(process) = guard.as_ref() else {
            return false;
        };
        if expected.is_some_and(|id| process.generation != id) {
            return false;
        }
        let generation = process.generation;
        self.generation.fetch_add(1, Ordering::SeqCst);
        let process = guard.take().unwrap();
        self.pending
            .lock()
            .unwrap()
            .retain(|_, pending| pending.generation != generation);
        // Keep launch serialized until the old library lock is released.
        terminate(process);
        true
    }

    pub fn shutdown(&self) {
        self.shutdown_generation(None);
    }
}

/// Run one action on the browser panel and send its result back to the service.
fn run_browser_action(app: AppHandle, event: Value) {
    let Some(call_id) = event.get("call_id").and_then(Value::as_str).map(str::to_owned) else {
        return;
    };
    let call = event.get("call").cloned().unwrap_or(Value::Null);
    tauri::async_runtime::spawn(async move {
        let result = app
            .state::<crate::browser::Browser>()
            .act(&app, "main", call)
            .await;
        // A reconnected or stopped service no longer waits; it drops the result.
        let _ = app
            .state::<Bridge>()
            .call(&app, "browser_result", json!({"call_id": call_id, "result": result}))
            .await;
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn blocked_stdin_cannot_block_process_shutdown() {
        let mut child = Command::new("/bin/sleep")
            .arg("30")
            .stdin(Stdio::piped())
            .spawn()
            .unwrap();
        let input = writer(&mut child).unwrap();
        input.try_send(vec![b'x'; 1024 * 1024]).unwrap();
        let started = std::time::Instant::now();
        terminate(Process {
            child,
            input,
            generation: 1,
        });
        assert!(started.elapsed() < Duration::from_secs(5));
    }
}
