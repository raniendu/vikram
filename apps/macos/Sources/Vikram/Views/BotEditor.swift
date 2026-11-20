import AppKit
import SwiftUI
import VikramKit

/// The same editor opens for new and existing bots: an icon and a name, a
/// system prompt, the tools it may use, a model, and when it runs.
struct BotEditor: View {
  @Environment(\.dismiss) private var dismiss
  @Environment(AppState.self) private var state
  @Environment(BotController.self) private var bots
  @Environment(ChatController.self) private var chats
  @State private var draft: BotDraft
  @State private var showingMore = false

  init(draft: BotDraft) { _draft = State(initialValue: draft) }

  private var availableModels: [LocalModel] {
    chats.models.filter { $0.available && (draft.cloud || !$0.isCloud) }
  }

  var body: some View {
    VStack(spacing: 0) {
      HStack(spacing: 12) {
        BotIcon(icon: draft.icon, name: draft.name, size: 36)
        Text(draft.botId == nil ? "New Bot" : "Edit Bot")
          .font(Theme.display(22, relativeTo: .title))
        Spacer()
      }.padding()
      Form {
        Section {
          TextField("Name", text: $draft.name, prompt: Text("e.g. Swift Evolution"))
            .accessibilityIdentifier("bot-name")
          LabeledContent("Icon") {
            BotIconPicker(icon: $draft.icon, name: draft.name)
          }
        }
        Section {
          TextField(
            "System prompt",
            text: $draft.charter,
            prompt: Text(
              "Check the Swift Evolution proposals every morning. Tell me when one is accepted or returned for revision, with a link and one line on why it matters."
            ),
            axis: .vertical
          )
          .labelsHidden()
          .lineLimit(6...14)
          .accessibilityIdentifier("bot-prompt")
        } header: {
          Text("System prompt")
        } footer: {
          Text("Its first sentence becomes the bot's description. Put any pages or sources to check here.")
            .font(.caption).foregroundStyle(Theme.muted)
        }
        Section {
          toolToggle("web_search", "Web search", "magnifyingglass")
          toolToggle("web_fetch", "Read web pages", "doc.text")
          toolToggle("sandbox", "Python sandbox", "shippingbox")
        } header: {
          Text("Tools")
        } footer: {
          Text(
            "A bot needs web search or Read web pages to check for news. The sandbox has no network and no access to your files. Bots run unattended, so they never use the browser, your Mac's tools, or integrations."
          )
          .font(.caption).foregroundStyle(Theme.muted)
        }
        Section("Model") {
          Picker("Model", selection: $draft.model) {
            Text("Default model").tag("")
            if !draft.model.isEmpty && !availableModels.contains(where: { $0.name == draft.model })
            {
              Text("\(formatModelName(draft.model)) · unavailable").tag(draft.model)
            }
            ForEach(availableModels) { Text($0.displayName).tag($0.name) }
          }
          Toggle("Allow OpenRouter models", isOn: $draft.cloud)
          if draft.cloud {
            Text(
              "Instructions and context go to OpenRouter and its provider. Charges may apply; there is no monetary cap. Connect in Workbench to list models. No automatic fallback."
            )
            .font(.caption).foregroundStyle(Theme.danger)
          }
        }
        Section {
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
            DatePicker("Time", selection: $draft.clock, displayedComponents: .hourAndMinute)
          }
        } header: {
          HStack {
            Text("Schedule")
            Spacer()
            Text(scheduleSummary).foregroundStyle(Theme.muted)
          }
        }
        Section {
          DisclosureGroup("More", isExpanded: $showingMore) {
            Picker("Notifications", selection: $draft.notify) {
              Text("Only needs attention").tag("attention")
              Text("Any new finding").tag("findings")
              Text("Home only").tag("never")
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
          }
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
          .buttonStyle(.glass).keyboardShortcut(.defaultAction)
          .disabled(draft.validationMessage != nil || bots.working || chats.offline)
          .accessibilityIdentifier("save-bot")
      }.padding()
    }
    .frame(width: 600, height: min(760, (NSScreen.main?.visibleFrame.height ?? 800) - 80))
    .onChange(of: draft.cloud) { _, allowed in
      if !allowed && draft.model.hasPrefix("openrouter:") { draft.model = "" }
    }
    .onAppear { bots.error = nil }
  }

  private func toolToggle(_ id: String, _ title: String, _ symbol: String) -> some View {
    Toggle(
      isOn: Binding(
        get: { draft.tools.contains(id) },
        set: { if $0 { draft.tools.insert(id) } else { draft.tools.remove(id) } }
      )
    ) {
      Label(title, systemImage: symbol)
    }
    .accessibilityIdentifier("bot-tool-\(id)")
  }

  private var visibleValidation: String? {
    guard !draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
      !draft.charter.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
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
