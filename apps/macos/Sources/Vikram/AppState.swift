import AppKit
import SwiftUI
import UniformTypeIdentifiers
import VikramKit

/// What the sidebar has selected: Home, the bots, the scheduled tasks, or a chat.
enum SidebarItem: Hashable {
    case home
    case bots
    case scheduled
    case chat(String)
}

/// A step into a scheduled task or one of its runs.
enum ScheduleRoute: Hashable {
    case task(String)
    case run(String)
}

/// A step into a bot or one of its runs.
enum BotRoute: Hashable {
    case bot(String)
    case run(String)
}

/// The full-window screens besides a chat.
enum Screen: Hashable {
    case chat
    case home
    case bots
    case scheduled
}

/// The app: its chat service, the controllers the window shows, the browser
/// panel, and quitting.
@MainActor @Observable
final class AppState {
    let service: ServiceConnection
    let chats: ChatController
    let schedules: ScheduleController
    let bots: BotController
    let updates: UpdateController
    let workbench: WorkbenchController
    let browser = BrowserEngine()

    /// Home first: the window opens on what the bots need from the person.
    var screen: Screen = .home
    var showsWorkbench = false
    var schedulePath: [ScheduleRoute] = []
    var botPath: [BotRoute] = []

    var showsScheduled: Bool {
        get { screen == .scheduled }
        set { if newValue { screen = .scheduled } else if screen == .scheduled { screen = .chat } }
    }
    /// The person hid the browser panel without closing the browser.
    var browserHidden = false
    private(set) var exiting = false
    private var started = false

    static var version: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
            ?? "development"
    }

    init() {
        let service = ServiceConnection(appVersion: Self.version)
        self.service = service
        let chats = ChatController(service: service)
        self.chats = chats
        workbench = WorkbenchController(service: service, chats: chats)
        schedules = ScheduleController(service: service)
        bots = BotController(service: service)
        updates = UpdateController(service: service)
        service.onEvent = { [weak self] event in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.receive(event) } }
        }
        service.onBrowser = { [weak self] callId, call in
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self?.runBrowserAction(callId, call) }
            }
        }
    }

    func start() async {
        guard !started else { return }
        started = true
        await chats.boot()
        guard !chats.offline else { return }
        try? await schedules.refresh()
        try? await bots.refresh()
        await updates.load()
        await workbench.refresh(chatId: chats.selected)
    }

    private func receive(_ event: JSONValue) {
        switch event["event"]?.string {
        case "update": updates.handle(event)
        case "schedules":
            // Bots share the scheduled-task file, so one event covers both.
            schedules.changed()
            bots.changed()
        default:
            if let event = try? event.decode(RuntimeEvent.self) {
                chats.handle(event)
                if event.event == "idle" || event.event == "queue" || event.event == "workbench" {
                    Task { await workbench.refresh(chatId: chats.selected) }
                }
                if event.event == "fatal" || event.event == "disconnected" {
                    browser.close()
                }
            }
        }
    }

    // MARK: Navigation

    var selection: SidebarItem? {
        get {
            switch screen {
            case .home: return .home
            case .bots: return .bots
            case .scheduled: return .scheduled
            case .chat: return chats.selected.map(SidebarItem.chat)
            }
        }
        set {
            switch newValue {
            case .home?:
                if screen != .home {
                    screen = .home
                    Task { try? await bots.refresh() }
                }
            case .bots?:
                if screen != .bots {
                    screen = .bots
                    botPath = []
                    Task { await bots.openBot(nil) }
                }
            case .scheduled?:
                if screen != .scheduled {
                    screen = .scheduled
                    schedulePath = []
                    Task { await schedules.openTask(nil) }
                }
            case .chat(let id)?:
                screen = .chat
                if id != chats.selected { Task { await chats.select(id) } }
            case nil:
                break
            }
        }
    }

    func newChat(folder: String? = nil) {
        screen = .chat
        Task { await chats.newChat(folder: folder) }
    }

    func openBot(_ id: String) {
        screen = .bots
        botPath = [.bot(id)]
    }

    /// A bot's run, from Home or the bot's page.
    func openBotRun(_ botId: String, _ runId: String) {
        screen = .bots
        botPath = [.bot(botId), .run(runId)]
    }

    func chooseModel(_ name: String) async {
        if name.hasPrefix("openrouter:"), name != chats.chat?.model {
            let alert = NSAlert()
            alert.messageText = "Use this OpenRouter model?"
            alert.informativeText = "Your active conversation history and attachments will be sent to OpenRouter and the selected model’s provider. This choice also becomes the default for future CLI and desktop conversations. Cloud usage may incur charges."
            alert.addButton(withTitle: "Use OpenRouter")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        await chats.setModel(name)
        await workbench.refresh(chatId: chats.selected)
    }

    func openChat(_ id: String) {
        screen = .chat
        Task { await chats.select(id) }
    }

    func openTask(_ id: String) {
        screen = .scheduled
        schedulePath = [.task(id)]
    }

    var canRetry: Bool {
        guard let chat = chats.chat, let last = chat.messages.last, !last.attempts.isEmpty,
            chats.active == nil, !chats.sending, !chats.offline, chats.model != nil
        else { return false }
        return !last.attempts.contains { $0.status == "running" || $0.status == "complete" }
    }

    var canReconnect: Bool {
        !chats.loading && chats.active == nil && !chats.sending && !chats.importing
    }

    func reconnect() async {
        browser.close()
        await chats.reconnect()
        if !chats.offline {
            try? await schedules.refresh()
            try? await bots.refresh()
        }
    }

    // MARK: Attachments

    static let attachmentTypes: [UTType] = [
        .png, .jpeg, .webP, .pdf, .plainText, .commaSeparatedText, .tabSeparatedText, .json,
        .yaml, .sourceCode, .html, .data,
    ]

    func pickAttachments() {
        guard chats.chat != nil else { return }
        let panel = NSOpenPanel()
        panel.title = "Attach Files"
        panel.message = "Images, PDF, Word, Excel, CSV, and text or code files, up to 10 MB each."
        panel.prompt = "Attach"
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = Self.attachmentTypes
        guard panel.runModal() == .OK else { return }
        let urls = panel.urls
        Task { await chats.importFiles(urls) }
    }

    // MARK: Browser

    /// Run one action from the service on the panel and send the result back.
    private func runBrowserAction(_ callId: String, _ call: JSONValue) {
        browserHidden = false
        let action = browser.enqueue(call)
        Task {
            let result = await action.value
            // A reconnected or stopped service no longer waits; it drops the result.
            _ = try? await service.call(
                "browser_result", ["call_id": .string(callId), "result": result])
        }
    }

    /// The person closed the browser: its private session ends, and the reply
    /// that was using it is told.
    func closeBrowser() {
        browser.close()
        browserHidden = false
        Task {
            do {
                _ = try await service.call("browser_closed")
            } catch {
                chats.error = "The browser could not be closed. Try again."
            }
        }
    }

    var browserShown: Binding<Bool> {
        Binding(
            get: { self.browser.isOpen && !self.browserHidden },
            set: { self.browserHidden = !$0 })
    }

    // MARK: Appearance

    var appearance: String { chats.preferences["appearance"]?.string ?? "system" }

    func applyAppearance() {
        NSApp.appearance =
            switch appearance {
            case "light": NSAppearance(named: .aqua)
            case "dark": NSAppearance(named: .darkAqua)
            default: nil
            }
    }

    // MARK: Quitting

    /// Ask before stopping a running reply, save drafts, and stop the service.
    func prepareToQuit(restarting: Bool) async -> Bool {
        let verb = restarting ? "Restart" : "Quit"
        if chats.active != nil || chats.sending {
            let alert = NSAlert()
            alert.messageText = "Stop the reply and \(verb.lowercased())?"
            alert.informativeText =
                "The active reply will stop. Saved messages and its last saved partial answer will be here when you return."
            alert.addButton(withTitle: "Stop and \(verb)")
            alert.addButton(withTitle: "Keep Open")
            guard alert.runModal() == .alertFirstButtonReturn else { return false }
        }
        do {
            try await chats.flushDrafts()
        } catch {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "\(verb) with unsaved changes?"
            alert.informativeText =
                "Your latest draft could not be saved. Copy it before \(restarting ? "restarting" : "quitting") to keep your text."
            alert.addButton(withTitle: "\(verb) Without Saving")
            alert.addButton(withTitle: "Keep Open")
            guard alert.runModal() == .alertFirstButtonReturn else { return false }
        }
        exiting = true
        browser.close()
        let service = service
        await Task.detached { service.shutDown() }.value
        return true
    }

    /// Quit, then open the app again once this process has exited.
    func restart() {
        Task {
            guard await prepareToQuit(restarting: true) else { return }
            if let bundle = Self.relaunchTarget() {
                let relaunch = Process()
                relaunch.executableURL = URL(fileURLWithPath: "/bin/sh")
                relaunch.arguments = [
                    "-c",
                    "while kill -0 \"$1\" 2>/dev/null; do sleep 0.2; done; exec /usr/bin/open \"$2\"",
                    "sh", String(ProcessInfo.processInfo.processIdentifier), bundle.path,
                ]
                relaunch.standardInput = FileHandle.nullDevice
                relaunch.standardOutput = FileHandle.nullDevice
                relaunch.standardError = FileHandle.nullDevice
                try? relaunch.run()
            }
            NSApp.terminate(nil)
        }
    }

    /// The installed app in ~/Applications, which updates keep current; else
    /// the bundle this process runs from.
    static func relaunchTarget() -> URL? {
        let installed = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Applications/Vikram.app")
        var directory: ObjCBool = false
        if FileManager.default.fileExists(atPath: installed.path, isDirectory: &directory),
            directory.boolValue
        {
            return installed
        }
        let bundle = Bundle.main.bundleURL
        return bundle.pathExtension == "app" ? bundle : nil
    }
}
