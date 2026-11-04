import AppKit
import SwiftUI
import VikramKit

struct VikramApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var state = AppState()

    var body: some Scene {
        Window("Vikram", id: "main") {
            ContentView()
                .environment(state)
                .environment(state.chats)
                .environment(state.schedules)
                .environment(state.updates)
                .environment(state.browser)
                .frame(minWidth: 760, minHeight: 540)
                .onAppear { delegate.state = state }
                .task { await state.start() }
        }
        .defaultSize(width: 1200, height: 820)
        .commands { AppCommands(state: state) }

        Settings {
            SettingsView()
                .environment(state)
                .environment(state.chats)
                .environment(state.updates)
        }
    }
}

struct AppCommands: Commands {
    let state: AppState

    var body: some Commands {
        CommandGroup(after: .appInfo) {
            Button("Check for Updates…") { Task { await state.updates.check() } }
                .disabled(state.updates.status?.state == "unavailable")
        }
        CommandGroup(replacing: .newItem) {
            Button("New Chat") { state.newChat() }
                .keyboardShortcut("n")
                .disabled(state.chats.offline || state.chats.loading)
        }
        SidebarCommands()
        InspectorCommands()
        CommandMenu("Chat") {
            Button("Send") { Task { await state.chats.send() } }
                .keyboardShortcut(.return, modifiers: .command)
                .disabled(!state.chats.canSend)
            Button("Stop Reply") { Task { await state.chats.stop() } }
                .keyboardShortcut(".")
                .disabled(state.chats.active == nil)
            Button("Retry with Selected Model") { Task { await state.chats.send(retry: true) } }
                .disabled(!state.canRetry)
            Divider()
            Toggle(
                "Plan Mode",
                isOn: Binding(
                    get: { state.chats.chat?.mode == .plan },
                    set: { on in Task { await state.chats.setMode(on ? .plan : .normal) } })
            )
            .keyboardShortcut("p", modifiers: [.command, .shift])
            .disabled(state.chats.chat == nil || state.chats.offline)
            Button("Attach Files…") { state.pickAttachments() }
                .keyboardShortcut("a", modifiers: [.command, .shift])
                .disabled(state.chats.chat == nil || state.chats.offline || state.chats.importing)
            Divider()
            Button("Refresh Models") { Task { await state.chats.refreshModels() } }
                .disabled(state.chats.offline || state.chats.refreshing)
            Button("Reconnect Chat Service") { Task { await state.reconnect() } }
                .disabled(!state.canReconnect)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    @MainActor weak var state: AppState?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // A development build runs outside an app bundle; give it a Dock icon.
        if Bundle.main.bundleIdentifier == nil {
            NSApp.setActivationPolicy(.regular)
            NSApp.activate()
        }
    }

    @MainActor
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let state, !state.exiting else { return .terminateNow }
        Task { @MainActor in
            let ready = await state.prepareToQuit(restarting: false)
            NSApp.reply(toApplicationShouldTerminate: ready)
        }
        return .terminateLater
    }
}
