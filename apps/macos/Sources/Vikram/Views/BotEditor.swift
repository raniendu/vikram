import AppKit
import SwiftUI
import VikramKit

/// One presentation for both template buttons and New Bot. The draft contains
/// the actual template defaults and is initialized before the sheet opens.
struct BotEditor: View {
  @Environment(\.dismiss) private var dismiss
  @Environment(AppState.self) private var state
  @Environment(BotController.self) private var bots
  @Environment(ChatController.self) private var chats
  @State private var draft: BotDraft

  init(draft: BotDraft) { _draft = State(initialValue: draft) }

  private var availableModels: [LocalModel] {
    chats.models.filter { $0.available && (draft.cloud || !$0.isCloud) }
  }

  var body: some View {
    VStack(spacing: 0) {
      HStack {
        Text(draft.botId == nil ? "New Bot" : "Edit Bot")
          .font(Theme.display(22, relativeTo: .title))
        Spacer()
      }.padding()
      Form {
        if draft.botId == nil {
          Picker(
            "Template",
            selection: Binding(
              get: { draft.templateId ?? "" },
              set: { id in
                guard let template = bots.templates.first(where: { $0.id == id }) else { return }
                do { try draft.apply(template) } catch { bots.error = error.localizedDescription }
              }
            )
          ) {
            ForEach(bots.templates.filter(\.available)) { Text($0.title).tag($0.id) }
          }
          Text(
            "Changing the template replaces its responsibility, instructions, rubric and schedule. Your name, sources and model are kept."
          )
          .font(.caption).foregroundStyle(Theme.muted)
        }
        TextField("Name", text: $draft.name, prompt: Text("Immigration Watch"))
          .accessibilityIdentifier("bot-name")
        TextField("Responsibility", text: $draft.mandate, axis: .vertical)
          .lineLimit(2...5).accessibilityIdentifier("bot-mandate")
        TextField(
          "Sources (one URL or search query per line)", text: $draft.sources, axis: .vertical
        )
        .lineLimit(2...6).accessibilityIdentifier("bot-sources")
        Section("When to check") {
          Picker("Repeat", selection: $draft.repeatKind) {
            Text("Daily").tag("daily")
            Text("Weekly").tag("weekly")
            Text("Interval").tag("interval")
            if !["daily", "weekly", "interval"].contains(draft.repeatKind) {
              Text("Keep existing schedule").tag(draft.repeatKind)
            }
          }
          if draft.repeatKind == "weekly" {
            HStack {
              ForEach(BotDraft.weekdays, id: \.self) { day in
                Toggle(
                  day.capitalized,
                  isOn: Binding(
                    get: { draft.days.contains(day) },
                    set: { if $0 { draft.days.insert(day) } else { draft.days.remove(day) } }
                  )
                ).toggleStyle(.button)
              }
            }
          }
          if draft.repeatKind == "interval" {
            Stepper(
              "Every \(draft.interval) minutes", value: $draft.interval, in: 15...10080, step: 15)
          } else if ["daily", "weekly"].contains(draft.repeatKind) {
            DatePicker("Local time", selection: $draft.clock, displayedComponents: .hourAndMinute)
          }
        }
        Section("Policy") {
          Picker("Model", selection: $draft.model) {
            Text("Inherit default bot's model").tag("")
            if !draft.model.isEmpty && !availableModels.contains(where: { $0.name == draft.model })
            {
              Text("\(formatModelName(draft.model)) · unavailable").tag(draft.model)
            }
            ForEach(availableModels) { Text($0.displayName).tag($0.name) }
          }
          Toggle("Allow OpenRouter for this bot", isOn: $draft.cloud)
          if draft.cloud {
            Text(
              "Instructions, sources and context go to OpenRouter and its provider. Charges may apply; there is no monetary cap. Connect in Workbench to list models. No automatic fallback."
            )
            .font(.caption).foregroundStyle(Theme.danger)
          }
          Stepper("Time budget: \(draft.budget) minutes", value: $draft.budget, in: 1...60)
          Toggle(
            "Flag overdue checks",
            isOn: Binding(
              get: { draft.expectedDays != nil },
              set: { draft.expectedDays = $0 ? 10 : nil }
            ))
          if let days = draft.expectedDays {
            Stepper(
              "Flag if no completed run in \(days) days",
              value: Binding(
                get: { draft.expectedDays ?? 10 }, set: { draft.expectedDays = $0 }
              ), in: 1...90)
          }
          Picker("macOS notifications", selection: $draft.notify) {
            Text("Only needs attention").tag("attention")
            Text("Any new finding").tag("findings")
            Text("Home only").tag("never")
          }
        }
        DisclosureGroup("Charter and materiality") {
          TextField("Standing instructions", text: $draft.charter, axis: .vertical).lineLimit(3...8)
          TextField("What is material?", text: $draft.rubric, axis: .vertical).lineLimit(3...6)
          TextField(
            "Keyword gates (one per line, optional)", text: $draft.keywords, axis: .vertical
          )
          .lineLimit(2...4)
          Text("Uncertain or off-keyword findings stay on the bot's page rather than Home.")
            .font(.caption).foregroundStyle(Theme.muted)
        }
        Text(
          "Unattended: search/fetch and the offline sandbox only. No browser, host tools, integrations or automatic actions."
        )
        .font(.caption).foregroundStyle(Theme.muted)
      }
      .formStyle(.grouped)
      HStack {
        if let error = bots.error ?? draft.validationMessage {
          Text(error).font(.callout).foregroundStyle(Theme.danger).lineLimit(3)
            .accessibilityIdentifier("bot-editor-error")
        }
        Spacer()
        Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
        Button(bots.working ? "Saving…" : draft.botId == nil ? "Create Bot" : "Save") { save() }
          .buttonStyle(.primary).keyboardShortcut(.defaultAction)
          .disabled(draft.validationMessage != nil || bots.working || chats.offline)
          .accessibilityIdentifier("save-bot")
      }.padding()
    }
    .frame(width: 640, height: min(680, (NSScreen.main?.visibleFrame.height ?? 780) - 100))
    .onChange(of: draft.cloud) { _, allowed in
      if !allowed && draft.model.hasPrefix("openrouter:") { draft.model = "" }
    }
    .onAppear { bots.error = nil }
  }

  private func save() {
    guard draft.validationMessage == nil, !bots.working else { return }
    if draft.cloud && !draft.originallyAllowedCloud {
      let alert = NSAlert()
      alert.messageText = "Allow unattended OpenRouter calls for this bot?"
      alert.informativeText =
        "Instructions, sources and context go to OpenRouter and its provider. Charges may apply; time and request limits are not a spending cap. This does not allow host access or automatic fallback."
      alert.addButton(withTitle: "Allow OpenRouter")
      alert.addButton(withTitle: "Cancel")
      guard alert.runModal() == .alertFirstButtonReturn else { return }
    }
    let fields = draft.fields
    Task {
      if let id = draft.botId {
        if await bots.update(id, fields: fields) { dismiss() }
      } else if let created = await bots.create(fields: fields) {
        dismiss()
        state.openBot(created.id)
      }
    }
  }
}
