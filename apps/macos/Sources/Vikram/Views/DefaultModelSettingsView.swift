import AppKit
import SwiftUI
import VikramKit

struct DefaultModelSettingsView: View {
    @Environment(ChatController.self) private var chats
    @Environment(WorkbenchController.self) private var workbench

    private var selected: String {
        let name = workbench.status?.defaultModel ?? ""
        return chats.models.first { $0.matches(name) }?.name ?? name
    }

    var body: some View {
        Picker("Default model on this Mac", selection: Binding(
            get: { selected },
            set: { choose($0) }))
        {
            if selected.isEmpty {
                Text("Choose a model").tag("")
            } else if !chats.models.contains(where: { $0.matches(selected) }) {
                Text("\(formatModelName(selected)) · unavailable").tag(selected)
                    .selectionDisabled(true)
            }
            ForEach(chats.models) { model in
                Text(model.displayName + " · " + model.providerLabel + (model.available ? "" : " · unavailable"))
                    .tag(model.name).selectionDisabled(!model.available)
            }
        }
        .disabled(workbench.status == nil || workbench.working || chats.offline || chats.active != nil || chats.sending)
        Text("Used for new CLI and desktop conversations. Choosing a model in a conversation also remembers it as the default on this Mac.")
            .font(.callout).foregroundStyle(Theme.muted)
        Button("Refresh Models") { Task { await chats.refreshModels() } }
            .disabled(chats.refreshing || chats.offline)
    }

    private func choose(_ name: String) {
        if chats.models.first(where: { $0.matches(name) })?.isCloud == true {
            let alert = NSAlert()
            alert.messageText = "Use OpenRouter by default?"
            alert.informativeText = "New CLI and desktop conversations and their attachments will be sent to OpenRouter and the selected model’s provider. Cloud usage may incur charges."
            alert.addButton(withTitle: "Save Default")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        Task { await workbench.perform("default_model", value: name) }
    }
}
