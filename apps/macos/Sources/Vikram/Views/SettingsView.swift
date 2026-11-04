import SwiftUI
import VikramKit

/// Settings (⌘,): appearance, local models, privacy, and updates.
struct SettingsView: View {
    @Environment(AppState.self) private var state
    @Environment(ChatController.self) private var chats
    @Environment(UpdateController.self) private var updates

    var body: some View {
        Form {
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
                        Button("Refresh") { Task { await chats.refreshModels() } }
                            .disabled(chats.refreshing || chats.offline)
                    }
                }
                Text(
                    chats.connection
                        ?? "\(chats.models.filter(\.available).count) local chat models available."
                )
                .foregroundStyle(Theme.muted)
                DisclosureGroup("Model Capabilities") {
                    ForEach(chats.models) { model in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(formatModelName(model.name)).fontWeight(.medium)
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
                                    "Ollama does not advertise adjustable thinking levels. Model default is used."
                                )
                                .font(.caption).foregroundStyle(Theme.muted)
                            }
                        }
                        .padding(.vertical, 2)
                    }
                }
                Button("Reconnect Chat Service") { Task { await state.reconnect() } }
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
                    "Chats and imported files are saved on this Mac. There is no cloud sync or analytics. The model can read attached content and use web search, but has no direct access to your files."
                )
                Text(
                    "Vikram’s browser has its own private session, with none of your other browsers’ cookies or passwords; closing it or quitting Vikram clears it. Vikram asks before it submits a form, signs in, buys, posts, or deletes, never types passwords or payment details, and cannot reach your local network. Take over the browser whenever you want to use the page yourself."
                )
                .foregroundStyle(Theme.muted)
                Text(
                    "Your full conversation is saved. A model’s available context may cover less of it; Vikram does not automatically summarize or enlarge that context."
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
                            } else if !["checking", "installing"].contains(status.state) {
                                Button("Check for Updates") { Task { await updates.check() } }
                            }
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .tint(Theme.accent)
        .frame(width: 560, height: 620)
    }
}
