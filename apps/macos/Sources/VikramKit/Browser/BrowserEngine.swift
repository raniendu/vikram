import AppKit
import Foundation
import Observation
import WebKit

/// Vikram's agent browser: one WebKit view the model drives.
///
/// In the app it is a panel beside the chat that the person watches and can
/// take over; for the CLI the same engine runs in a hidden window
/// (`vikram-desktop --browser-helper`). Either way it has its own
/// non-persistent data store, refuses non-web schemes and private network
/// addresses, never downloads, never opens windows, and runs Vikram's page
/// script in a separate script world the page cannot reach.
@MainActor @Observable
public final class BrowserEngine: NSObject {
    /// Whether the browser has a page open (the panel shows while it does).
    public private(set) var isOpen = false
    public private(set) var url = ""
    public private(set) var title = ""
    public private(set) var loading = false

    /// The page view, while the browser is open.
    @ObservationIgnored public private(set) var webView: WKWebView?
    /// Called with each new page view, so a host can show it.
    @ObservationIgnored public var onCreate: ((WKWebView) -> Void)?

    @ObservationIgnored private var loads = 0
    @ObservationIgnored private var notices: [String] = []
    @ObservationIgnored private var observations: [NSKeyValueObservation] = []
    @ObservationIgnored private var tail: Task<Void, Never>?
    @ObservationIgnored private let size: CGSize

    static let shotWidth: CGFloat = 1024
    static let scriptTimeout: Double = 10
    static let loadTimeout: Double = 20
    static let rulesIdentifier = "vikram-private-network"

    /// WebKit discards a script world, with the page script's element numbers,
    /// once nothing holds it; this one lives as long as the process.
    private static let world = WKContentWorld.world(name: "vikram")

    public init(size: CGSize = CGSize(width: 720, height: 900)) {
        self.size = size
    }

    // MARK: Lifecycle

    private func ensure() async throws -> WKWebView {
        if let webView { return webView }
        guard PageScript.source != nil else {
            throw ServiceError("The browser is missing its page script. Reinstall Vikram.")
        }
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.preferences.isElementFullscreenEnabled = false
        // Keep working while the panel is hidden or the window is in the background.
        configuration.preferences.inactiveSchedulingPolicy = .none
        configuration.allowsAirPlayForMediaPlayback = false
        // Without the private-network blocker the browser does not start.
        guard let rules = await Self.compileRules() else {
            throw ServiceError(
                "The browser's network protection could not start, so the browser stays closed.")
        }
        configuration.userContentController.add(rules)
        let view = WKWebView(frame: CGRect(origin: .zero, size: size), configuration: configuration)
        view.navigationDelegate = self
        view.uiDelegate = self
        view.allowsMagnification = true
        view.isInspectable = false
        view.setAccessibilityLabel("Vikram's browser")
        observations = [
            view.observe(\.title, options: [.new]) { [weak self] view, _ in
                MainActor.assumeIsolated { self?.title = (view.title ?? "").bounded(300) }
            },
            view.observe(\.url, options: [.new]) { [weak self] view, _ in
                MainActor.assumeIsolated {
                    self?.url = (view.url?.absoluteString ?? "").bounded(2048)
                }
            },
            view.observe(\.isLoading, options: [.new]) { [weak self] view, _ in
                MainActor.assumeIsolated { self?.loading = view.isLoading }
            },
        ]
        view.load(URLRequest(url: URL(string: "about:blank")!))
        webView = view
        isOpen = true
        onCreate?(view)
        return view
    }

    private static func compileRules() async -> WKContentRuleList? {
        guard let store = WKContentRuleListStore.default() else { return nil }
        return try? await store.compileContentRuleList(
            forIdentifier: rulesIdentifier, encodedContentRuleList: BrowserPolicy.privateRules)
    }

    /// Close the browser; its private session ends with it.
    public func close() {
        observations = []
        webView?.stopLoading()
        webView?.navigationDelegate = nil
        webView?.uiDelegate = nil
        webView?.removeFromSuperview()
        webView = nil
        isOpen = false
        url = ""
        title = ""
        loading = false
        notices = []
    }

    /// Show `html` as if it came from `base`, for offline tests of the page script.
    func load(html: String, base: URL) async throws {
        let view = try await ensure()
        let before = loads
        view.loadHTMLString(html, baseURL: base)
        await settle(view, since: before, navigating: true)
    }

    private func note(_ text: String) {
        if notices.count < 6 && !notices.contains(text) { notices.append(text) }
    }

    // MARK: Actions

    /// Run one action and describe the page afterwards, as `EngineResult` JSON.
    /// Actions run one at a time, in the order they arrive.
    public func act(_ call: JSONValue) async -> JSONValue {
        await enqueue(call).value
    }

    /// Queue one action behind the others now; its task returns the result.
    public func enqueue(_ call: JSONValue) -> Task<JSONValue, Never> {
        let previous = tail
        let task = Task { @MainActor [weak self] () -> JSONValue in
            await previous?.value
            guard let self else { return Self.failure("The browser closed.") }
            return await self.perform(call)
        }
        tail = Task { _ = await task.value }
        return task
    }

    private func perform(_ call: JSONValue) async -> JSONValue {
        let action: BrowserAction
        let screenshot: Bool
        do {
            (action, screenshot) = try BrowserPolicy.parse(call)
        } catch {
            return Self.failure(error)
        }
        let view: WKWebView
        do {
            view = try await ensure()
        } catch {
            return Self.failure(error)
        }
        do {
            return try await run(view, action, screenshot: screenshot)
        } catch {
            // A failed action still shows where the browser is, when it can.
            var result = Self.failure(error)
            if let page = try? await observe(view, screenshot: false)["page"] {
                result = result.setting("page", page)
            }
            return result
        }
    }

    static func failure(_ error: Error) -> JSONValue {
        failure((error as? ServiceError)?.message ?? "The action failed.")
    }

    static func failure(_ message: String) -> JSONValue {
        ["ok": false, "error": .string(message.bounded(900))]
    }

    private func run(_ view: WKWebView, _ action: BrowserAction, screenshot: Bool) async throws
        -> JSONValue
    {
        let before = loads
        var opened: (from: String, to: URL)?
        let outcome: JSONValue
        let navigating: Bool
        switch action {
        case .open(let url):
            let from = self.url
            view.load(URLRequest(url: url))
            await settle(view, since: before, navigating: true)
            opened = (from, url)
            (outcome, navigating) = (["status": "done"], false)
        case .click(let element, let approved):
            outcome = try await script(
                "click(element, approved)", ["element": element, "approved": approved])
            navigating = true
        case .type(let element, let text, let submit, let approved):
            outcome = try await script(
                "type(element, text, submit, approved)",
                ["element": element, "text": text, "submit": submit, "approved": approved])
            navigating = submit
        case .scroll(let up):
            outcome = try await script("scroll(direction)", ["direction": up ? "up" : "down"])
            navigating = false
        case .back:
            outcome = try await script("back()", [:])
            navigating = true
        case .look:
            (outcome, navigating) = (["status": "done"], false)
        }
        let target = outcome["target"]?.string?.bounded(280)
        switch outcome["status"]?.string {
        case "done":
            break
        case "approval":
            let page: JSONValue = [
                "url": .string((outcome["url"]?.string ?? "").bounded(2048)),
                "title": .string((outcome["title"]?.string ?? "").bounded(300)),
            ]
            return [
                "ok": false, "approval": outcome["approval"] ?? .null, "target": target.json,
                "page": page,
            ]
        case "refused":
            return try await observe(view, screenshot: false)
                .setting("ok", false)
                .setting("refused", outcome["error"] ?? .null)
                .setting("target", target.json)
        default:
            throw ServiceError(outcome["error"]?.string ?? "The action failed.")
        }
        if let notice = outcome["notice"]?.string { note(notice) }
        switch action {
        case .open, .look: break
        default: await settle(view, since: before, navigating: navigating)
        }
        var result = try await observe(view, screenshot: screenshot)
        if let opened {
            // WebKit keeps the previous page when a load fails.
            let now = result["page"]?["url"]?.string ?? ""
            if !now.isEmpty, now == opened.from, opened.from != opened.to.absoluteString {
                var page = result["page"]?.object ?? [:]
                let list = page["notices"]?.array ?? []
                page["notices"] = .array(
                    ["The page did not open: it may be unreachable, blocked, or too slow."] + list)
                result = result.setting("page", .object(page))
            }
        }
        if let target { result = result.setting("target", .string(target)) }
        return result
    }

    /// A click may or may not navigate: give a load a moment to start, then
    /// wait for it (or a failure) to finish, and let the page's scripts render.
    private func settle(_ view: WKWebView, since before: Int, navigating: Bool) async {
        let began = Date()
        let startWindow: Double = navigating ? 2 : 0.7
        while true {
            let started = loads > before
            let busy = view.isLoading
            if (started || busy) && !busy { break }
            let elapsed = Date().timeIntervalSince(began)
            if !started && !busy && elapsed > startWindow { break }
            if elapsed > Self.loadTimeout {
                note("The page was still loading; this is what had appeared.")
                break
            }
            try? await Task.sleep(for: .milliseconds(100))
        }
        try? await Task.sleep(for: .milliseconds(300))
    }

    private func observe(_ view: WKWebView, screenshot: Bool) async throws -> JSONValue {
        var page = try await script("observe()", [:])
        let pending = notices
        notices = []
        if var object = page.object {
            let list = (pending.map(JSONValue.string) + (object["notices"]?.array ?? []))
                .prefix(10)
            object["notices"] = .array(Array(list))
            page = .object(object)
        }
        var result: JSONValue = ["ok": true, "page": page]
        if screenshot, let image = await snapshot(view) {
            result = result.setting("screenshot", .string(image.base64EncodedString()))
        }
        return result
    }

    /// Run Vikram's page script in its own script world and parse what it returns.
    /// Values reach the script only as arguments, never as code.
    private func script(_ call: String, _ arguments: [String: any Sendable]) async throws
        -> JSONValue
    {
        guard let view = webView, let source = PageScript.source else {
            throw ServiceError("The browser closed.")
        }
        let body = "\(source)\nreturn JSON.stringify(await vikram.\(call));"
        let text: String = try await deadline(
            Self.scriptTimeout, ServiceError("The page did not respond in time.")
        ) {
            let value: Any?
            do {
                value = try await view.callAsyncJavaScript(
                    body, arguments: arguments, in: nil, contentWorld: Self.world)
            } catch {
                throw ServiceError(
                    "The page script failed (\(error.localizedDescription.bounded(200))).")
            }
            guard let text = value as? String else {
                throw ServiceError("The page script returned nothing.")
            }
            return text
        }
        guard let value = try? JSONDecoder().decode(JSONValue.self, from: Data(text.utf8)) else {
            throw ServiceError("The page script returned an unreadable result.")
        }
        return value
    }

    /// The visible part of the page as a JPEG about 1024 pixels wide.
    private func snapshot(_ view: WKWebView) async -> Data? {
        let configuration = WKSnapshotConfiguration()
        let scale = view.window?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 2
        configuration.snapshotWidth = NSNumber(value: Double(Self.shotWidth / max(scale, 1)))
        // Only the JPEG bytes leave the closure: older SDKs do not mark NSImage Sendable.
        let jpeg = try? await deadline(5, ServiceError("Snapshot timed out.")) { () -> Data? in
            let image = try await view.takeSnapshot(configuration: configuration)
            guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff)
            else { return nil }
            return bitmap.representation(using: .jpeg, properties: [.compressionFactor: 0.6])
        }
        return jpeg ?? nil
    }
}

extension BrowserEngine: WKNavigationDelegate {
    public func webView(
        _ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
        preferences: WKWebpagePreferences
    ) async -> (WKNavigationActionPolicy, WKWebpagePreferences) {
        guard let url = action.request.url, BrowserPolicy.allowed(url) else {
            let scheme = (action.request.url?.scheme ?? "unknown").bounded(20)
            note("A link to a blocked address was not opened (\(scheme)).")
            return (.cancel, preferences)
        }
        if action.shouldPerformDownload {
            note("A download was blocked: Vikram's browser never saves files.")
            return (.cancel, preferences)
        }
        return (.allow, preferences)
    }

    public func webView(
        _ webView: WKWebView, decidePolicyFor response: WKNavigationResponse
    ) async -> WKNavigationResponsePolicy {
        let disposition =
            (response.response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Disposition")?.lowercased() ?? ""
        if !response.canShowMIMEType || disposition.hasPrefix("attachment") {
            note("A download was blocked: Vikram's browser never saves files.")
            return .cancel
        }
        return .allow
    }

    public func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!)
    {
        loads += 1
        loading = true
    }

    public func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loading = webView.isLoading
    }

    public func webView(
        _ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error
    ) {
        loading = webView.isLoading
    }

    public func webView(
        _ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        loading = webView.isLoading
    }

    public func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        note("The page stopped unexpectedly; open it again to continue.")
        loading = false
    }
}

extension BrowserEngine: WKUIDelegate {
    public func webView(
        _ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
        for action: WKNavigationAction, windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        note("A new window or pop-up was blocked.")
        return nil
    }
}

/// Vikram's page script (`browser.js`), from the app's resources or, in
/// development and tests, from the package.
enum PageScript {
    static let source: String? = {
        if let url = Bundle.main.url(forResource: "browser", withExtension: "js"),
            let text = try? String(contentsOf: url, encoding: .utf8)
        {
            return text
        }
        let package = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        return try? String(
            contentsOf: package.appendingPathComponent("Resources/browser.js"), encoding: .utf8)
    }()
}

@MainActor
private final class DeadlineGate {
    var done = false
}

/// The result of `operation`, or `error` once `seconds` pass. WebKit calls
/// cannot be cancelled, so a late result is dropped rather than awaited.
@MainActor
func deadline<T: Sendable>(
    _ seconds: Double, _ error: Error & Sendable,
    _ operation: @escaping @MainActor () async throws -> T
) async throws -> T {
    let gate = DeadlineGate()
    return try await withCheckedThrowingContinuation { continuation in
        Task { @MainActor in
            do {
                let value = try await operation()
                if !gate.done {
                    gate.done = true
                    continuation.resume(returning: value)
                }
            } catch let failure {
                if !gate.done {
                    gate.done = true
                    continuation.resume(throwing: failure)
                }
            }
        }
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(seconds))
            if !gate.done {
                gate.done = true
                continuation.resume(throwing: error)
            }
        }
    }
}

extension JSONValue {
    /// This object with `key` set to `value`.
    func setting(_ key: String, _ value: JSONValue) -> JSONValue {
        var object = self.object ?? [:]
        object[key] = value
        return .object(object)
    }
}
