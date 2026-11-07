import AppKit
import SwiftUI
import VikramKit

struct WorkbenchView: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(ChatController.self) private var chats
    @Environment(WorkbenchController.self) private var workbench

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label("Workbench", systemImage: "hammer")
                    .font(Theme.display(22, weight: .medium, relativeTo: .title2))
                Spacer()
                if workbench.working { ProgressView().controlSize(.small) }
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(20)
            if let error = workbench.error {
                Text(error).font(.callout).foregroundStyle(Theme.danger)
                    .textSelection(.enabled).padding([.horizontal, .bottom], 16)
            }
            TabView {
                WorkspaceSettingsView().tabItem { Label("Workspace", systemImage: "folder") }
                ProviderSettingsView().tabItem { Label("Models", systemImage: "cpu") }
                SessionWorkbenchView().tabItem {
                    Label("Conversation", systemImage: "point.3.connected.trianglepath.dotted")
                }
                IntegrationsView().tabItem { Label("Integrations", systemImage: "puzzlepiece.extension") }
            }
        }
        .frame(width: 690, height: 650)
        .tint(Theme.accent)
        .task(id: chats.selected) { await workbench.refresh(chatId: chats.selected) }
    }
}

private struct WorkspaceSettingsView: View {
    @Environment(ChatController.self) private var chats
    @Environment(WorkbenchController.self) private var workbench

    var body: some View {
        Form {
            Section("Workspace") {
                Text(workbench.status?.workspace ?? "Choose the project folder for this conversation.")
                    .font(.body.monospaced()).textSelection(.enabled)
                Button("Choose Folder…", systemImage: "folder") { chooseFolder() }
                    .disabled(workbench.working || chats.active != nil)
                if workbench.status?.workspace != nil {
                    Toggle(
                        "Trust this workspace",
                        isOn: Binding(
                            get: { workbench.status?.trusted ?? false },
                            set: { trusted in
                                Task {
                                    await workbench.perform("trust", options: ["trusted": .bool(trusted)])
                                }
                            })
                    )
                    .disabled(workbench.working || chats.active != nil)
                }
                Text(
                    "Trust enables host coding tools with your user privileges. Vikram asks before operations outside the workspace, destructive actions, credential access, package installation, and remote changes. Plan mode allows inspection only."
                )
                .font(.callout).foregroundStyle(Theme.muted)
            }
            Section("Project Resources") {
                Text(
                    "Reload instructions, skills, templates, and settings after changing them. Project settings cannot grant permissions."
                )
                .foregroundStyle(Theme.muted)
                Button("Reload Resources", systemImage: "arrow.clockwise") {
                    Task { await workbench.perform("reload") }
                }
                .disabled(workbench.working)
                if let resources = workbench.status?.resources, !resources.isEmpty {
                    DisclosureGroup("Loaded resources") {
                        ForEach(resources.keys.sorted(), id: \.self) { key in
                            LabeledContent(key.replacingOccurrences(of: "_", with: " ").capitalized) {
                                Text(resourceSummary(resources[key])).font(.caption).textSelection(.enabled)
                            }
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Use Workspace"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await workbench.perform("workspace", value: url.path) }
    }
}

struct ProviderSettingsView: View {
    @Environment(ChatController.self) private var chats
    @Environment(WorkbenchController.self) private var workbench
    @State private var apiKey = ""

    var body: some View {
        Form {
            Section("Default Model") {
                DefaultModelSettingsView()
            }
            Section("Local Ollama") {
                LabeledContent("Endpoint", value: "localhost:11434")
                Text("Ollama remains the default. Local models never fall back to a cloud provider.")
                    .foregroundStyle(Theme.muted)
                Button("Refresh Models", systemImage: "arrow.clockwise") {
                    Task { await chats.refreshModels() }
                }.disabled(chats.refreshing || chats.offline)
            }
            Section("OpenRouter") {
                Toggle(
                    "Show OpenRouter models",
                    isOn: Binding(
                        get: { workbench.status?.cloudEnabled ?? false },
                        set: { enabled in
                            Task { await workbench.perform("cloud", options: ["enabled": .bool(enabled)]) }
                        })
                )
                .disabled(workbench.working)
                Text(
                    "Selecting an OpenRouter model sends your active conversation history and attachments to OpenRouter and the selected model’s provider. Cloud usage may incur charges."
                )
                .font(.callout).foregroundStyle(Theme.muted)
                LabeledContent("Credential", value: credentialLabel)
                SecureField("OpenRouter API key", text: $apiKey)
                    .textContentType(.password)
                    .onSubmit { connect() }
                HStack {
                    Button("Save in Keychain") { connect() }
                        .disabled(
                            apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                || workbench.working)
                    Button("Remove Saved Key") {
                        apiKey = ""
                        Task { await workbench.perform("logout") }
                    }
                    .disabled(workbench.status?.credentials == nil || workbench.working)
                }
                if workbench.status?.credentials == "environment" {
                    Text(
                        "OPENROUTER_API_KEY overrides Keychain. Unset it in the launching environment to remove that credential."
                    )
                    .font(.caption).foregroundStyle(Theme.muted)
                }
            }
        }
        .formStyle(.grouped)
        .onDisappear { apiKey = "" }
    }

    private var credentialLabel: String {
        switch workbench.status?.credentials {
        case "environment": "Environment variable"
        case "keychain": "macOS Keychain"
        default: "Not connected"
        }
    }

    private func connect() {
        let key = apiKey
        apiKey = ""
        guard !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        Task { await workbench.perform("login", value: key) }
    }
}

func resourceSummary(_ value: JSONValue?) -> String {
    guard let value, value != .null else { return "Unknown" }
    if let string = value.string { return String(string.prefix(500)) }
    if let array = value.array { return "\(array.count) items" }
    if let object = value.object { return "\(object.count) entries" }
    if let bool = value.bool { return bool ? "Yes" : "No" }
    if let number = value.number { return number.formatted() }
    return "Unknown"
}
