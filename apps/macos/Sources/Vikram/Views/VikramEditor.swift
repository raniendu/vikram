import AppKit
import SwiftUI
import VikramKit

/// Vikram, the default bot behind every conversation: its icon, system
/// prompt, tools, and default model. Its name stays Vikram.
struct VikramEditor: View {
  @Environment(\.dismiss) private var dismiss
  @Environment(BotController.self) private var bots
  @Environment(ChatController.self) private var chats
  @Environment(WorkbenchController.self) private var workbench
  @State private var icon: String?
  @State private var instructions: String
  @State private var tools: Set<String>
  @State private var model: String
  private let original: Assistant

  init(assistant: Assistant) {
    original = assistant
    _icon = State(initialValue: assistant.icon)
    _instructions = State(initialValue: assistant.instructions)
    _tools = State(initialValue: Set(assistant.tools))
    _model = State(initialValue: assistant.model)
  }

  private var selectedModel: String {
    chats.models.first { $0.matches(model) }?.name ?? model
  }

  var body: some View {
    VStack(spacing: 0) {
      HStack(spacing: 12) {
        BotIcon(icon: icon, name: "Vikram", size: 36)
        VStack(alignment: .leading, spacing: 2) {
          Text("Vikram").font(Theme.display(22, relativeTo: .title))
          Text("Your default bot · every conversation").font(.callout)
            .foregroundStyle(Theme.muted)
        }
        Spacer()
      }.padding()
      Form {
        Section {
          LabeledContent("Icon") { BotIconPicker(icon: $icon, name: "Vikram") }
        }
        Section {
          TextField(
            "System prompt", text: $instructions,
            prompt: Text("e.g. Answer briefly. I work in Swift and Python on macOS."),
            axis: .vertical
          )
          .labelsHidden()
          .lineLimit(5...12)
          .accessibilityIdentifier("vikram-prompt")
        } header: {
          Text("System prompt")
        } footer: {
          Text(
            "Added to every conversation, before any project instructions. Leave it empty to use Vikram as it is."
          )
          .font(.caption).foregroundStyle(Theme.muted)
        }
        Section {
          toolToggle("web_search", "Web search", "magnifyingglass")
          toolToggle("web_fetch", "Read web pages", "doc.text")
          toolToggle("sandbox", "Python sandbox", "shippingbox")
          toolToggle("browser", "Browser", "globe")
          toolToggle("tasks", "Scheduled tasks", "calendar.badge.clock")
        } header: {
          Text("Tools")
        } footer: {
          Text(
            "Folder access and its tools are chosen per conversation in the Workbench. Tools need a model that supports them."
          )
          .font(.caption).foregroundStyle(Theme.muted)
        }
        Section("Model") {
          Picker("Default model", selection: $model) {
            if selectedModel.isEmpty {
              Text("Choose a model").tag("")
            } else if !chats.models.contains(where: { $0.matches(selectedModel) }) {
              Text("\(formatModelName(selectedModel)) · unavailable").tag(selectedModel)
                .selectionDisabled(true)
            }
            ForEach(chats.models) { item in
              Text(item.displayName + " · " + item.providerLabel)
                .tag(item.name).selectionDisabled(!item.available)
            }
          }
          .disabled(chats.active != nil || chats.sending)
        }
      }
      .formStyle(.grouped)
      HStack {
        if let error = bots.error ?? workbench.error {
          Text(error).font(.callout).foregroundStyle(Theme.danger).lineLimit(3)
        }
        Spacer()
        Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
          .buttonStyle(.glass)
        Button(bots.working ? "Saving…" : "Save") { save() }
          .buttonStyle(.glass).keyboardShortcut(.defaultAction)
          .disabled(instructions.count > 6000 || bots.working || chats.offline)
          .accessibilityIdentifier("save-vikram")
      }.padding()
    }
    .frame(width: 600, height: min(720, (NSScreen.main?.visibleFrame.height ?? 800) - 80))
    .onAppear {
      bots.error = nil
      // Show the saved default under the name the model list uses.
      model = selectedModel
    }
  }

  private func toolToggle(_ id: String, _ title: String, _ symbol: String) -> some View {
    Toggle(
      isOn: Binding(
        get: { tools.contains(id) },
        set: { if $0 { tools.insert(id) } else { tools.remove(id) } })
    ) { Label(title, systemImage: symbol) }
  }

  private func save() {
    let originalName = chats.models.first { $0.matches(original.model) }?.name ?? original.model
    let changedModel = !model.isEmpty && model != originalName
    if changedModel, chats.models.first(where: { $0.matches(model) })?.isCloud == true {
      let alert = NSAlert()
      alert.messageText = "Use OpenRouter by default?"
      alert.informativeText =
        "New conversations and their attachments will be sent to OpenRouter and the selected model’s provider. Cloud usage may incur charges."
      alert.addButton(withTitle: "Save Default")
      alert.addButton(withTitle: "Cancel")
      guard alert.runModal() == .alertFirstButtonReturn else { return }
    }
    Task {
      if changedModel {
        guard await workbench.perform("default_model", value: model) != nil else { return }
      }
      let saved = await bots.saveAssistant(
        icon: icon,
        instructions: instructions.trimmingCharacters(in: .whitespacesAndNewlines),
        tools: Assistant.allTools.filter(tools.contains))
      if saved { dismiss() }
    }
  }
}
