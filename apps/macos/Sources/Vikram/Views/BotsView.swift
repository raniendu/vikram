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
  @State private var editor: BotDraft?

  var body: some View {
    @Bindable var state = state
    NavigationStack(path: $state.botPath) {
      List {
        Section {
          if !bots.loaded {
            if bots.loading || bots.error == nil {
              ProgressView("Loading bots…")
            } else {
              Text("Bots could not be loaded. Your saved bots have not been removed.")
              Button("Try Again") { refresh() }
            }
          } else {
            Text(backgroundNote).foregroundStyle(Theme.muted)
            if bots.bots.isEmpty {
            Text("No bots yet. Create one to monitor a topic on a schedule.")
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
        }
        BotTemplatesSection(templates: bots.templates, enabled: !chats.offline && !bots.working) {
          editor = bots.draft(templateId: $0.id)
        }
        if let error = bots.templateError {
          Section("Templates unavailable") {
            Text(error).foregroundStyle(Theme.danger)
            Button("Reload Templates") { refresh() }.disabled(bots.loading)
          }
        }
      }
      .scrollContentBackground(.hidden)
      .navigationTitle("Bots")
      .navigationDestination(for: BotRoute.self) { route in
        switch route {
        case .bot: BotDetail(edit: { editor = BotDraft(bot: $0) })
        case .run: BotRunDetail()
        }
      }
      .toolbar {
        Button("Refresh Bots", systemImage: "arrow.clockwise") { refresh() }
          .disabled(chats.offline || bots.loading)
        Button("Import Bot", systemImage: "square.and.arrow.down") { importBot() }
          .disabled(chats.offline || bots.working)
        Button("New Bot", systemImage: "plus") { editor = bots.draft() }
          .disabled(chats.offline || bots.working || !bots.templates.contains(where: \.available))
          .accessibilityIdentifier("new-bot")
      }
    }
    .task(id: chats.offline) {
      if !chats.offline && (!bots.loaded || bots.templates.isEmpty) { try? await bots.refresh() }
    }
    .task(id: state.botPath) {
      switch state.botPath.last {
      case .bot(let id)?: await bots.openBot(id)
      case .run(let id)?:
        if case .bot(let botId)? = state.botPath.dropLast().last { await bots.openBot(botId) }
        await schedules.openRun(id)
      case nil: await bots.openBot(nil)
      }
    }
    .sheet(item: $editor) { BotEditor(draft: $0) }
    .safeAreaInset(edge: .top, spacing: 0) { BotErrorBanner() }
  }

  private func refresh() { Task { try? await bots.refresh() } }

  private var backgroundNote: String {
    switch bots.background {
    case "on":
      "Bots run on this Mac even when Vikram is closed. Only findings that need you notify by default."
    case "failed": "The background service could not start. Reopen Vikram and try again."
    case "unavailable":
      "Background runs need the installed app. In development use `vikram bots run NAME`."
    default:
      "Create a bot to begin. Unattended runs use search, page fetching, and the offline sandbox only."
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
      Task { if let bot = await bots.importBot(value) { state.openBot(bot.id) } }
    } catch { bots.error = "Could not read this bot configuration." }
  }
}

/// Real controls, not informational rows. Unavailable templates remain visibly
/// disabled, with a reason, and cannot bypass the server's capability policy.
struct BotTemplatesSection: View {
  let templates: [BotTemplate]
  let enabled: Bool
  let select: (BotTemplate) -> Void

  var body: some View {
    Section("Templates") {
      ForEach(templates) { template in
        Button {
          guard template.available && enabled else { return }
          select(template)
        } label: {
          HStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
              Text(template.title).fontWeight(.medium)
              Text(template.summary).foregroundStyle(Theme.muted)
              if let reason = template.reason {
                Text("Unavailable: \(reason)").font(.caption).foregroundStyle(Theme.muted)
              }
            }
            Spacer()
            Label(
              template.available ? "Use Template" : "Unavailable",
              systemImage: template.available ? "plus.circle" : "lock"
            )
            .foregroundStyle(template.available ? Theme.accent : Theme.muted)
          }
          .frame(maxWidth: .infinity, alignment: .leading)
          .contentShape(Rectangle())
          .padding(.vertical, 8)
        }
        .buttonStyle(.plain)
        .disabled(!template.available || !enabled)
        .accessibilityIdentifier("bot-template-\(template.id)")
        .accessibilityLabel(
          template.available ? "Use \(template.title) template" : "\(template.title), unavailable"
        )
        .help(template.reason ?? "Configure a new \(template.title) bot")
      }
    }
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
    if let error = bots.openError {
      VStack(spacing: 12) {
        Text("Could not open bot").font(.headline)
        Text(error).foregroundStyle(Theme.muted)
        Button("Try Again") {
          if case .bot(let id)? = state.botPath.last { Task { await bots.openBot(id) } }
        }
      }.padding()
    } else if let bot = bots.bot {
      let running = bot.runs?.contains { $0.status == "running" } ?? false
      List {
        Section("Overview") {
          Text(bot.mandate).font(Theme.display(18, relativeTo: .headline))
          LabeledContent("Schedule", value: bot.description)
          if let summary = bot.lastSummary { Text(summary).textSelection(.enabled) }
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
        DisclosureGroup("Settings") {
          LabeledContent("Model", value: bot.model.map(formatModelName) ?? "Inherit default")
          LabeledContent("OpenRouter", value: bot.cloud ? "Explicitly allowed" : "Not allowed")
          LabeledContent("Time budget", value: "\(bot.budgetMinutes) minutes per run")
          LabeledContent("Notifications", value: bot.notify)
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
    if let error = schedules.error {
      VStack(spacing: 12) {
        Text("Could not open run").font(.headline)
        Text(error).foregroundStyle(Theme.muted)
        Button("Try Again") {
          if case .run(let id)? = state.botPath.last { Task { await schedules.openRun(id) } }
        }
      }.padding()
    } else if let run = schedules.run {
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
