import SwiftUI
import VikramKit

/// Settings (⌘,): the configuration the app shares with the CLI.
struct SettingsView: View {
    @Environment(WorkbenchController.self) private var workbench

    var body: some View {
        VStack(spacing: 0) {
            if let error = workbench.error {
                Text(error).font(.callout).foregroundStyle(Theme.danger)
                    .textSelection(.enabled).padding(12)
            }
            TabView {
                GeneralSettingsView().tabItem { Label("General", systemImage: "gearshape") }
                ProviderSettingsView().tabItem { Label("Models", systemImage: "cpu") }
                WorkspaceSettingsView().tabItem { Label("Workspace", systemImage: "folder") }
                IntegrationsView().tabItem {
                    Label("Integrations", systemImage: "puzzlepiece.extension")
                }
            }
        }
        .frame(width: 640, height: 640)
    }
}

private struct GeneralSettingsView: View {
    @Environment(AppState.self) private var state
    @Environment(ChatController.self) private var chats
    @Environment(UpdateController.self) private var updates

    var body: some View {
        Form {
            Section {
                DefaultModelSettingsView()
            } header: {
                Text("Default Model")
            } footer: {
                Text("Saved as `model` in settings.json beside your sessions. The vikram CLI reads the same file.")
            }
            Section("Appearance") {
                Picker(
                    "Appearance",
                    selection: Binding(
                        get: { state.appearance },
                        set: { value in
                            Task {
                                await chats.preference("appearance", .string(value))
                                state.applyAppearance()
                            }
                        })
                ) {
                    Text("System").tag("system")
                    Text("Light").tag("light")
                    Text("Dark").tag("dark")
                }
                .pickerStyle(.segmented)
            }
            Section {
                LabeledContent("Ollama") {
                    HStack {
                        Circle()
                            .fill(chats.connection == nil && !chats.offline ? Theme.success : Theme.danger)
                            .frame(width: 8, height: 8)
                            .accessibilityHidden(true)
                        Text("localhost:11434").font(.body.monospaced())
                    }
                }
                Text(
                    chats.connection
                        ?? "\(chats.models.filter { $0.available && !$0.isCloud }.count) local chat models available."
                )
                .foregroundStyle(Theme.muted)
                DisclosureGroup("Model Capabilities") {
                    ForEach(chats.models) { model in
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(model.displayName) · \(model.providerLabel)").fontWeight(.medium)
                            Text(model.name).font(.caption.monospaced()).foregroundStyle(Theme.muted)
                            Text(
                                !model.available
                                    ? model.reason ?? "Unavailable"
                                    : (model.vision ? "Text and images" : "Text")
                                        + (model.canThink ? " · thinking" : "")
                            )
                            .font(.caption)
                            if model.available && model.canThink && model.thinking.isEmpty {
                                Text(
                                    "This model does not advertise adjustable thinking levels. Model default is used."
                                )
                                .font(.caption).foregroundStyle(Theme.muted)
                            }
                        }
                        .padding(.vertical, 2)
                    }
                }
                Button("Reconnect Chat Service") { Task { await state.reconnect() } }
                    .buttonStyle(.glass)
                    .disabled(!state.canReconnect)
            } header: {
                Text("Local Models")
            } footer: {
                Text("Vikram uses the chat models you installed in Ollama on this Mac.")
            }
            Section("Web Search") {
                Text(
                    "Models that support tools can search and read public pages through Parallel’s free service whenever needed, subject to rate limits. The model decides when to search. Queries may include conversation or attachment details. Original files stay on your Mac."
                )
                .foregroundStyle(Theme.muted)
            }
            Section("Private by Design") {
                Text(
                    "Chats and imported files are saved on this Mac. There is no cloud sync or analytics. OpenRouter is optional and sends active history to the selected provider. Trusted workspaces enable host coding tools with your user privileges."
                )
                Text(
                    "Vikram’s browser has its own private session, with none of your other browsers’ cookies or passwords; closing it or quitting Vikram clears it. Vikram asks before it submits a form, signs in, buys, posts, or deletes, never types passwords or payment details, and cannot reach your local network. Take over the browser whenever you want to use the page yourself."
                )
                .foregroundStyle(Theme.muted)
                Text(
                    "Your full conversation is saved. Vikram compacts context at 80% of a known model limit while preserving the transcript and complete tool exchanges. You can also compact it from Workbench."
                )
                .foregroundStyle(Theme.muted)
            }
            Section("About") {
                LabeledContent("Version", value: AppState.version)
                if let status = updates.status, status.state != "unavailable" {
                    LabeledContent("Updates") {
                        HStack {
                            Text(updates.text).foregroundStyle(Theme.muted)
                            if status.state == "ready" {
                                Button("Restart") { state.restart() }
                                    .buttonStyle(.glassProminent)
                            } else if !["checking", "installing"].contains(status.state) {
                                Button("Check for Updates") { Task { await updates.check() } }
                                    .buttonStyle(.glass)
                            }
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .task { await state.workbench.refresh(chatId: chats.selected) }
    }
}
