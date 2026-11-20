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
  @State private var editingVikram: Assistant?

  var body: some View {
    @Bindable var state = state
    NavigationStack(path: $state.botPath) {
      List {
        if let vikram = bots.assistant {
          Section {
            VikramRow(assistant: vikram) { editingVikram = vikram }
          }
        }
        Section {
          if !bots.loaded {
            if bots.loading || bots.error == nil {
              ProgressView("Loading bots…")
            } else {
              Text("Bots could not be loaded. Your saved bots have not been removed.")
              Button("Try Again") { refresh() }
            }
          } else if bots.bots.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
              Text("No scheduled bots yet").font(Theme.display(18, relativeTo: .headline))
              Text("A bot watches something on a schedule and tells you only what matters.")
                .foregroundStyle(Theme.muted)
              Button("New Bot", systemImage: "plus") { create() }
                .buttonStyle(.glass)
                .controlSize(.large)
                .disabled(!canCreate)
            }
            .padding(.vertical, 8)
          } else {
            ForEach(bots.bots) { bot in
              NavigationLink(value: BotRoute.bot(bot.id)) {
                VStack(alignment: .leading, spacing: 4) {
                  BotHealthRow(bot: bot)
                  Text(bot.mandate).font(.callout).foregroundStyle(Theme.muted)
                }
              }
            }
          }
        } header: {
          if bots.assistant != nil { Text("Scheduled bots") }
        } footer: {
          if bots.loaded {
            Text(backgroundNote).font(.caption).foregroundStyle(Theme.muted)
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
        Button("New Bot", systemImage: "plus") { create() }
          .disabled(!canCreate)
          .accessibilityIdentifier("new-bot")
      }
    }
    .task(id: chats.offline) {
      if !chats.offline && !bots.loaded { try? await bots.refresh() }
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
    .sheet(item: $editingVikram) { VikramEditor(assistant: $0) }
    .safeAreaInset(edge: .top, spacing: 0) { BotErrorBanner() }
  }

  private func refresh() { Task { try? await bots.refresh() } }

  private func create() { editor = bots.draft() }

  private var canCreate: Bool {
    !chats.offline && !bots.working
  }

  private var backgroundNote: String {
    switch bots.background {
    case "on":
      "Bots run on this Mac even when Vikram is closed. Only findings that need you notify by default."
    case "failed": "The background service could not start. Reopen Vikram and try again."
    case "unavailable":
      "Background runs need the installed app. In development use `vikram bots run NAME`."
    default:
      "Unattended runs use search, page fetching, and the offline sandbox only."
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

/// Vikram, the default bot, always first: it answers every conversation.
private struct VikramRow: View {
  @Environment(BotController.self) private var bots
  @Environment(ChatController.self) private var chats
  let assistant: Assistant
  let edit: () -> Void

  var body: some View {
    HStack(spacing: 12) {
      BotIcon(icon: assistant.icon, name: "Vikram", size: 34)
      VStack(alignment: .leading, spacing: 3) {
        HStack(spacing: 6) {
          Text("Vikram").fontWeight(.semibold)
          Text("Default").font(.caption).foregroundStyle(Theme.muted)
            .padding(.horizontal, 6).padding(.vertical, 1)
            .overlay { Capsule().strokeBorder(Theme.rule) }
        }
        Text(summary).font(.callout).foregroundStyle(Theme.muted).lineLimit(2)
      }
      Spacer()
      Button("Edit…", action: edit)
        .buttonStyle(.glass)
        .disabled(bots.working || chats.offline)
        .accessibilityIdentifier("edit-vikram")
    }
    .padding(.vertical, 6)
    .contentShape(Rectangle())
    .onTapGesture(count: 2, perform: edit)
  }

  private var summary: String {
    let prompt = assistant.instructions.trimmingCharacters(in: .whitespacesAndNewlines)
    let first = prompt.split(whereSeparator: \.isNewline).first.map(String.init)
    let model = assistant.model.isEmpty ? "No default model" : formatModelName(assistant.model)
    let tools = "\(assistant.tools.count) of \(Assistant.allTools.count) tools"
    return [first ?? "Answers your conversations", model, tools].joined(separator: " · ")
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
          HStack(spacing: 12) {
            BotIcon(bot, size: 40)
            Text(bot.mandate).font(Theme.display(18, relativeTo: .headline))
          }
          LabeledContent("Schedule", value: bot.description)
          LabeledContent("CLI") {
            Text("vikram bots show \(bot.name.contains(" ") ? "\"\(bot.name)\"" : bot.name)")
              .font(.callout.monospaced()).textSelection(.enabled)
          }
          if let summary = bot.lastSummary { Text(summary).textSelection(.enabled) }
        }
        Section {
          if let prompt = bot.config?["charter"]?.string, !prompt.isEmpty {
            DisclosureGroup("System prompt") {
              Text(prompt).font(.callout).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
          }
          LabeledContent("Tools", value: toolsLabel(bot.tools))
          LabeledContent("Model", value: bot.model.map(formatModelName) ?? "Default model")
          LabeledContent("OpenRouter", value: bot.cloud ? "Explicitly allowed" : "Not allowed")
          LabeledContent("Time budget", value: "\(bot.budgetMinutes) minutes per run")
          LabeledContent("Notifications", value: notifyLabel(bot.notify))
        } header: {
          HStack {
            Text("Configuration")
            Spacer()
            Button("Edit…") { edit(bot) }
              .buttonStyle(.glass).controlSize(.small)
              .disabled(running || bots.working || chats.offline)
          }
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

  private func toolsLabel(_ tools: [String]?) -> String {
    let names = ["web_search": "Web search", "web_fetch": "Read web pages", "sandbox": "Python sandbox"]
    let chosen = (tools ?? BotDraft.allTools).compactMap { names[$0] }
    return chosen.isEmpty ? "None" : ListFormatter.localizedString(byJoining: chosen)
  }

  private func notifyLabel(_ value: String) -> String {
    ["attention": "Only needs attention", "findings": "Any new finding", "never": "Home only"][value]
      ?? value
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
