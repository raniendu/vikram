import AppKit
import SwiftUI
import UniformTypeIdentifiers
import VikramKit

/// Create and manage specialists independently of conversations.
struct BotsView: View {
  @Environment(AppState.self) private var state
  @Environment(BotController.self) private var bots
  @Environment(ScheduleController.self) private var schedules
  @Environment(ChatController.self) private var chats
  @State private var creating = false
  @State private var editing: Bot?

  var body: some View {
    @Bindable var state = state
    NavigationStack(path: $state.botPath) {
      List {
        Section {
          Text(backgroundNote).foregroundStyle(Theme.muted)
          if bots.bots.isEmpty {
            Text("No bots yet. Choose a template and give it a responsibility.")
          }
          ForEach(bots.bots) { bot in
            NavigationLink(value: BotRoute.bot(bot.id)) {
              VStack(alignment: .leading, spacing: 4) {
                BotHealthRow(bot: bot)
                Text(bot.mandate).font(.callout).foregroundStyle(Theme.muted)
              }
            }
          }
        }
        Section("Templates") {
          ForEach(bots.templates) { template in
            VStack(alignment: .leading, spacing: 4) {
              Text(template.title).fontWeight(.medium)
              Text(template.summary).foregroundStyle(Theme.muted)
              if let reason = template.reason {
                Text("Blocked: \(reason)").font(.caption).foregroundStyle(Theme.danger)
              }
            }
            .padding(.vertical, 4)
          }
        }
      }
      .scrollContentBackground(.hidden)
      .navigationTitle("Bots")
      .navigationDestination(for: BotRoute.self) { route in
        switch route {
        case .bot: BotDetail(edit: { editing = $0 })
        case .run: BotRunDetail()
        }
      }
      .toolbar {
        Button("Import Bot", systemImage: "square.and.arrow.down") { importBot() }
          .disabled(chats.offline || bots.working)
        Button("New Bot", systemImage: "plus") { creating = true }
          .disabled(chats.offline || bots.working)
      }
    }
    .task(id: state.botPath) {
      switch state.botPath.last {
      case .bot(let id)?: await bots.openBot(id)
      case .run(let id)?:
        if case .bot(let botId)? = state.botPath.dropLast().last {
          await bots.openBot(botId)
        }
        await schedules.openRun(id)
      case nil: await bots.openBot(nil)
      }
    }
    .sheet(isPresented: $creating) { BotEditor(bot: nil) }
    .sheet(item: $editing) { BotEditor(bot: $0) }
    .safeAreaInset(edge: .top, spacing: 0) { BotErrorBanner() }
  }

  private var backgroundNote: String {
    switch bots.background {
    case "on":
      "Bots run on this Mac even when Vikram is closed. Only findings that need you notify by default."
    case "failed": "The background service could not start. Reopen Vikram and try again."
    case "unavailable":
      "Background runs need the installed app. In development use `vikram bots run NAME`."
    default:
      "Create a bot to begin. Unattended runs use search, page fetching, and the offline Python sandbox only."
    }
  }

  private func importBot() {
    let panel = NSOpenPanel()
    panel.allowedContentTypes = [.json]
    panel.message =
      "Import a bot configuration. OpenRouter permission is stripped. The imported bot starts paused."
    guard panel.runModal() == .OK, let url = panel.url else { return }
    do {
      let data = try Data(contentsOf: url)
      guard data.count <= 64 * 1024 else {
        bots.error = "Bot configurations must be smaller than 64 KiB."
        return
      }
      let value = try JSONDecoder().decode(JSONValue.self, from: data)
      Task {
        if let bot = await bots.importBot(value) { state.openBot(bot.id) }
      }
    } catch { bots.error = "Could not read this bot configuration." }
  }
}

private struct BotDetail: View {
  @Environment(AppState.self) private var state
  @Environment(BotController.self) private var bots
  @Environment(ChatController.self) private var chats
  let edit: (Bot) -> Void
  @State private var deleting = false
  @State private var resetting = false

  var body: some View {
    if let bot = bots.bot {
      let running = bot.runs?.contains { $0.status == "running" } ?? false
      List {
        Section("Responsibility") {
          Text(bot.mandate).font(Theme.display(18, relativeTo: .headline))
          LabeledContent("Schedule", value: bot.description)
          LabeledContent("Model", value: bot.model.map(formatModelName) ?? "Inherit default")
          LabeledContent("OpenRouter", value: bot.cloud ? "Explicitly allowed" : "Not allowed")
          LabeledContent("Budget", value: "\(bot.budgetMinutes) minutes per run")
          LabeledContent("Notifications", value: bot.notify)
          if let summary = bot.lastSummary {
            Text(summary).textSelection(.enabled)
          }
          Text(
            "Sandbox only. Search and fetch, no browser, no host tools, no integrations. Findings do not authorize actions."
          )
          .font(.caption).foregroundStyle(Theme.muted)
        }
        Section("Findings") {
          ForEach(bot.findings ?? []) { FindingCard(finding: $0) }
          if bot.findings?.isEmpty ?? true {
            Text("No open findings.").foregroundStyle(Theme.muted)
          }
        }
        Section("Runs") {
          ForEach(bot.runs ?? []) { run in
            NavigationLink(value: BotRoute.run(run.id)) {
              VStack(alignment: .leading, spacing: 4) {
                HStack {
                  Text(botOutcome(run.outcome ?? run.status)).fontWeight(.medium)
                  Text(when(run.started)).foregroundStyle(Theme.muted)
                }
                if let report = run.report ?? run.error {
                  Text(report).lineLimit(3).font(.callout).foregroundStyle(Theme.muted)
                }
              }
            }
          }
          if bot.runs?.isEmpty ?? true { Text("No runs yet.").foregroundStyle(Theme.muted) }
        }
      }
      .scrollContentBackground(.hidden)
      .navigationTitle(bot.name)
      .toolbar {
        Button(bot.runRequested ? "Starting…" : "Run Now", systemImage: "play") {
          Task { await bots.runNow(bot.id) }
        }.disabled(running || bot.runRequested || bots.working || chats.offline)
        Button(bot.enabled ? "Pause" : "Resume", systemImage: bot.enabled ? "pause" : "play.circle")
        {
          Task { await bots.setPaused(bot.id, bot.enabled) }
        }.disabled(bots.working || chats.offline)
        Menu("Bot Actions", systemImage: "ellipsis") {
          Button("Edit…") { edit(bot) }.disabled(running)
          Button("Export Configuration…") { export(bot) }
          Button("Reset Baseline…") { resetting = true }.disabled(running)
          Divider()
          Button("Delete…", role: .destructive) { deleting = true }.disabled(running)
        }.disabled(bots.working || chats.offline)
      }
      .confirmationDialog("Delete “\(bot.name)” and its findings?", isPresented: $deleting) {
        Button("Delete Bot", role: .destructive) {
          Task { if await bots.remove(bot.id) { state.botPath = [] } }
        }
      } message: {
        Text("Its configuration, findings, and run history will be permanently removed.")
      }
      .confirmationDialog("Reset what “\(bot.name)” remembers?", isPresented: $resetting) {
        Button("Reset Baseline", role: .destructive) { Task { await bots.reset(bot.id) } }
      } message: {
        Text(
          "Existing findings become resolved. The next run establishes a fresh baseline; run evidence remains."
        )
      }
    } else {
      ProgressView("Opening bot…")
    }
  }

  private func export(_ bot: Bot) {
    Task {
      guard let value = await bots.export(bot.id) else { return }
      let panel = NSSavePanel()
      panel.allowedContentTypes = [.json]
      panel.nameFieldStringValue = "\(bot.name).json"
      guard panel.runModal() == .OK, let url = panel.url else { return }
      do { try value.encoded().write(to: url, options: .atomic) } catch {
        bots.error = "Could not save the bot configuration."
      }
    }
  }
}

private struct BotRunDetail: View {
  @Environment(AppState.self) private var state
  @Environment(ScheduleController.self) private var schedules
  @Environment(BotController.self) private var bots
  @Environment(ChatController.self) private var chats

  var body: some View {
    if let run = schedules.run {
      ScrollView {
        VStack(alignment: .leading, spacing: 16) {
          Text(botOutcome(run.summary.outcome ?? run.summary.status))
            .font(Theme.display(24, relativeTo: .title))
          if let report = run.summary.report { Text(report).textSelection(.enabled) }
          UserMessage(text: run.prompt, attachments: [], label: "Run context")
          AnswerView(attempt: run.attempt, openTask: { state.openTask($0) })
        }
        .frame(maxWidth: 750).padding(28).frame(maxWidth: .infinity)
      }
      .navigationTitle("\(bots.bot?.name ?? "Bot") · \(when(run.summary.started))")
      .toolbar {
        if run.summary.status != "running" {
          Button("Discuss This Run", systemImage: "bubble.left.and.text.bubble.right") {
            Task {
              if let chat = await schedules.continueInChat(run.id) {
                state.screen = .chat
                await chats.adopt(chat)
              }
            }
          }.disabled(chats.offline || schedules.working)
        }
      }
    } else {
      ProgressView("Opening evidence…")
    }
  }
}

/// Typed configuration, created without a conversation. Advanced instructions
/// stay separate from sources, scheduling, materiality, and execution policy.
private struct BotEditor: View {
  @Environment(\.dismiss) private var dismiss
  @Environment(AppState.self) private var state
  @Environment(BotController.self) private var bots
  @Environment(ChatController.self) private var chats
  let bot: Bot?
  @State private var loaded = false
  @State private var name = ""
  @State private var mandate = ""
  @State private var template = "topic-watcher"
  @State private var sources = ""
  @State private var charter = ""
  @State private var rubric = ""
  @State private var keywords = ""
  @State private var repeatKind = "weekly"
  @State private var days: Set<String> = ["mon"]
  @State private var clock = Calendar.current.date(from: DateComponents(hour: 8)) ?? .now
  @State private var interval = 60
  @State private var model = ""
  @State private var cloud = false
  @State private var budget = 10
  @State private var expectedDays = 10
  @State private var notify = "attention"

  private let weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
  private var availableModels: [LocalModel] {
    chats.models.filter { $0.available && (cloud || !$0.isCloud) }
  }
  private var valid: Bool {
    !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      && !mandate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
      && (repeatKind != "weekly" || !days.isEmpty)
  }

  var body: some View {
    VStack(spacing: 0) {
      Form {
        if bot == nil {
          Picker("Template", selection: $template) {
            ForEach(bots.templates.filter(\.available)) { Text($0.title).tag($0.id) }
          }
        }
        TextField("Name", text: $name, prompt: Text("Immigration Watch"))
        TextField(
          "Responsibility", text: $mandate,
          prompt: Text("Tell me about official visa rule changes that affect me."), axis: .vertical
        )
        .lineLimit(2...5)
        TextField("Sources (one URL or search query per line)", text: $sources, axis: .vertical)
          .lineLimit(2...6)
        Section("When to check") {
          Picker("Repeat", selection: $repeatKind) {
            Text("Daily").tag("daily")
            Text("Weekly").tag("weekly")
            Text("Interval").tag("interval")
            if !["daily", "weekly", "interval"].contains(repeatKind) {
              Text("Keep existing schedule").tag(repeatKind)
            }
          }
          if repeatKind == "weekly" {
            HStack {
              ForEach(weekdays, id: \.self) { day in
                Toggle(
                  day.capitalized,
                  isOn: Binding(
                    get: { days.contains(day) },
                    set: { if $0 { days.insert(day) } else { days.remove(day) } }
                  )
                ).toggleStyle(.button)
              }
            }
          }
          if repeatKind == "interval" {
            Stepper("Every \(interval) minutes", value: $interval, in: 15...10080, step: 15)
          } else if ["daily", "weekly"].contains(repeatKind) {
            DatePicker("Local time", selection: $clock, displayedComponents: .hourAndMinute)
          }
        }
        Section("Policy") {
          Picker("Model", selection: $model) {
            Text("Inherit default bot's model").tag("")
            if !model.isEmpty && !availableModels.contains(where: { $0.name == model }) {
              Text("\(formatModelName(model)) · unavailable").tag(model)
            }
            ForEach(availableModels) { Text($0.displayName).tag($0.name) }
          }
          Toggle("Allow OpenRouter for this bot", isOn: $cloud)
          if cloud {
            Text(
              "The bot's instructions, sources, and context will go to OpenRouter and the selected provider. Charges may apply; there is no monetary cap. Connect OpenRouter in Workbench to list models. No automatic cloud fallback."
            )
            .font(.caption).foregroundStyle(Theme.danger)
          }
          Stepper("Time budget: \(budget) minutes", value: $budget, in: 1...60)
          Stepper(
            "Flag if no completed run in \(expectedDays) days", value: $expectedDays, in: 1...90)
          Picker("macOS notifications", selection: $notify) {
            Text("Only needs attention").tag("attention")
            Text("Any new finding").tag("findings")
            Text("Home only").tag("never")
          }
        }
        DisclosureGroup("Charter and materiality") {
          TextField("Standing instructions", text: $charter, axis: .vertical).lineLimit(3...8)
          TextField("What is material?", text: $rubric, axis: .vertical).lineLimit(3...6)
          TextField("Keyword gates (one per line, optional)", text: $keywords, axis: .vertical)
            .lineLimit(2...4)
          Text(
            "Low-confidence or off-keyword findings are held on the bot's page. Templates provide a charter and rubric when these fields are left empty."
          )
          .font(.caption).foregroundStyle(Theme.muted)
        }
        Text(
          "Unattended: search and fetch plus the offline sandbox only. No browser, host tools, integrations, or automatic actions."
        )
        .font(.caption).foregroundStyle(Theme.muted)
      }
      .formStyle(.grouped)
      HStack {
        if let error = bots.error { Text(error).foregroundStyle(Theme.danger).lineLimit(3) }
        Spacer()
        Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
        Button(bot == nil ? "Create Bot" : "Save") { save() }
          .buttonStyle(.primary).keyboardShortcut(.defaultAction)
          .disabled(!valid || bots.working)
      }.padding()
    }
    .frame(width: 650, height: 730)
    .onAppear { load() }
    .onChange(of: cloud) { _, allowed in
      if !allowed && model.hasPrefix("openrouter:") { model = "" }
    }
  }

  private func lines(_ text: String) -> [String] {
    text.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter {
      !$0.isEmpty
    }
  }

  private func load() {
    guard !loaded else { return }
    loaded = true
    guard let bot else { return }
    name = bot.name
    mandate = bot.mandate
    template = bot.template ?? "topic-watcher"
    model = bot.model ?? ""
    cloud = bot.cloud
    budget = bot.budgetMinutes
    expectedDays = bot.expectedDays ?? 10
    notify = bot.notify
    if let config = bot.config {
      sources = config["sources"]?.array?.compactMap(\.string).joined(separator: "\n") ?? ""
      charter = config["charter"]?.string ?? ""
      rubric = config["materiality"]?["rubric"]?.string ?? ""
      keywords =
        config["materiality"]?["keywords"]?.array?.compactMap(\.string).joined(separator: "\n")
        ?? ""
    }
    repeatKind = bot.schedule.repeatKind
    days = Set(bot.schedule.days ?? ["mon"])
    interval = bot.schedule.everyMinutes ?? 60
    if let time = bot.schedule.time {
      let parts = time.split(separator: ":").compactMap { Int($0) }
      if parts.count == 2 {
        clock =
          Calendar.current.date(from: DateComponents(hour: parts[0], minute: parts[1])) ?? .now
      }
    }
  }

  private func save() {
    if cloud && bot?.cloud != true {
      let alert = NSAlert()
      alert.messageText = "Allow unattended OpenRouter calls for this bot?"
      alert.informativeText =
        "Bot instructions, sources, and context will be sent to OpenRouter and the chosen provider. Usage may incur charges; time and request limits are not a dollar spending cap. You can pause the bot at any time. This does not permit automatic fallback or host access."
      alert.addButton(withTitle: "Allow OpenRouter")
      alert.addButton(withTitle: "Cancel")
      guard alert.runModal() == .alertFirstButtonReturn else { return }
    }
    let components = Calendar.current.dateComponents([.hour, .minute], from: clock)
    let time = String(format: "%02d:%02d", components.hour ?? 8, components.minute ?? 0)
    var schedule = ScheduleTime(
      repeatKind: repeatKind, time: time,
      days: weekdays.filter(days.contains), everyMinutes: interval
    ).fields
    if !["daily", "weekly", "interval"].contains(repeatKind), let old = bot?.schedule {
      schedule = old.fields
    }
    var fields: [String: JSONValue] = [
      "name": .string(name), "mandate": .string(mandate),
      "sources": .array(lines(sources).map(JSONValue.string)),
      "schedule": schedule, "model": model.isEmpty ? .null : .string(model),
      "cloud": .bool(cloud), "budget_minutes": .number(Double(budget)),
      "expected_days": .number(Double(expectedDays)), "notify": .string(notify),
    ]
    if bot == nil { fields["template"] = .string(template) }
    if !charter.isEmpty || bot != nil { fields["charter"] = .string(charter) }
    var materiality: [String: JSONValue] = [
      "keywords": .array(lines(keywords).map(JSONValue.string))
    ]
    if !rubric.isEmpty || bot != nil { materiality["rubric"] = .string(rubric) }
    fields["materiality"] = .object(materiality)
    Task {
      if let bot {
        if await bots.update(bot.id, fields: .object(fields)) { dismiss() }
      } else if let created = await bots.create(fields: .object(fields)) {
        dismiss()
        state.openBot(created.id)
      }
    }
  }
}
