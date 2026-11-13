import AppKit
import SwiftUI
import VikramKit

/// The same editor opens for new bots, templates, and existing bots.
struct BotEditor: View {
  @Environment(\.dismiss) private var dismiss
  @Environment(AppState.self) private var state
  @Environment(BotController.self) private var bots
  @Environment(ChatController.self) private var chats
  @State private var draft: BotDraft
  @State private var editingSchedule = false
  @State private var showingAdvanced = false

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
        TextField("Name", text: $draft.name, prompt: Text("e.g. macOS Updates"))
          .accessibilityIdentifier("bot-name")
        VStack(alignment: .leading, spacing: 6) {
          Text("What should it watch?").accessibilityHidden(true)
          TextField(
            "What should it watch?",
            text: $draft.mandate,
            prompt: Text("Check Apple’s release notes and tell me when a new macOS update ships."),
            axis: .vertical
          )
          .labelsHidden()
          .textFieldStyle(.roundedBorder)
          .lineLimit(3...5)
          .accessibilityIdentifier("bot-mandate")
        }
        if needsSources { sourcesField }
        DisclosureGroup(isExpanded: $editingSchedule) {
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
        } label: {
          LabeledContent("Schedule", value: scheduleSummary)
        }
        DisclosureGroup("Advanced", isExpanded: $showingAdvanced) {
          if draft.botId == nil {
            Picker(
              "Template",
              selection: Binding(
                get: { draft.templateId ?? "" },
                set: { id in
                  guard id != draft.templateId else { return }
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
          if !needsSources { sourcesField }
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
          TextField("Standing instructions", text: $draft.charter, axis: .vertical).lineLimit(3...8)
          TextField("What is material?", text: $draft.rubric, axis: .vertical).lineLimit(3...6)
          TextField(
            "Keyword gates (one per line, optional)", text: $draft.keywords, axis: .vertical
          )
          .lineLimit(2...4)
          Text("Uncertain or off-keyword findings stay on the bot's page rather than Home.")
            .font(.caption).foregroundStyle(Theme.muted)
        }
      }
      .formStyle(.grouped)
      HStack {
        if let error = bots.error ?? visibleValidation {
          Text(error).font(.callout).foregroundStyle(Theme.danger).lineLimit(3)
            .accessibilityIdentifier("bot-editor-error")
        }
        Spacer()
        Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
          .buttonStyle(.glass)
        Button(bots.working ? "Saving…" : draft.botId == nil ? "Create Bot" : "Save") { save() }
          .buttonStyle(.glassProminent).keyboardShortcut(.defaultAction)
          .disabled(draft.validationMessage != nil || bots.working || chats.offline)
          .accessibilityIdentifier("save-bot")
      }.padding()
    }
    .frame(width: 560, height: min(editorHeight, (NSScreen.main?.visibleFrame.height ?? 780) - 100))
    .onChange(of: draft.cloud) { _, allowed in
      if !allowed && draft.model.hasPrefix("openrouter:") { draft.model = "" }
    }
    .onAppear { bots.error = nil }
  }

  private var needsSources: Bool {
    draft.templateId == "page-watcher" || draft.templateId == "release-watcher"
  }

  private var sourcesField: some View {
    TextField("Sources", text: $draft.sources, prompt: Text("One URL or search query per line"), axis: .vertical)
      .lineLimit(2...6)
      .accessibilityIdentifier("bot-sources")
  }

  private var editorHeight: CGFloat {
    if showingAdvanced { return 670 }
    return (editingSchedule ? 490 : 380) + (needsSources ? 65 : 0)
  }

  private var visibleValidation: String? {
    guard !draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
      !draft.mandate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    else { return nil }
    return draft.validationMessage
  }

  private var scheduleSummary: String {
    let time = draft.clock.formatted(date: .omitted, time: .shortened)
    switch draft.repeatKind {
    case "daily": return "Daily at \(time)"
    case "weekly":
      let symbols = Calendar.current.weekdaySymbols
      let days = BotDraft.weekdays.enumerated().compactMap { index, day in
        draft.days.contains(day) ? symbols[(index + 1) % 7] : nil
      }
      guard !days.isEmpty else { return "Choose weekdays" }
      if days.count == 7 { return "Daily at \(time)" }
      if days.count > 3 { return "\(days.count) days a week at \(time)" }
      return "\(ListFormatter.localizedString(byJoining: days)) at \(time)"
    case "interval":
      let interval = Duration.seconds(draft.interval * 60)
        .formatted(.units(allowed: [.hours, .minutes], width: .abbreviated))
      return "Every \(interval)"
    default: return draft.savedScheduleDescription ?? "Existing schedule"
    }
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
