import SwiftUI
import VikramKit

/// The primary surface: decisions and failures first, material developments
/// second, one quiet health line for each bot. Chats remain a click away.
struct HomeView: View {
  @Environment(AppState.self) private var state
  @Environment(BotController.self) private var bots
  @Environment(ChatController.self) private var chats

  var body: some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 26) {
        VStack(alignment: .leading, spacing: 8) {
          Text(bots.home.isCalm ? "You're caught up." : "A few things need you.")
            .font(Theme.display(32, relativeTo: .largeTitle))
          Text("Your bots work in the background. Only what matters belongs here.")
            .foregroundStyle(Theme.muted)
        }
        if !bots.loaded {
          ProgressView("Loading your bots…")
        } else {
          VStack(alignment: .leading, spacing: 12) {
            Eyebrow("Needs you")
            ForEach(bots.home.stale) { bot in
              HStack {
                Image(systemName: "clock.badge.exclamationmark")
                  .foregroundStyle(Theme.danger)
                Text(
                  "\(bot.name) has not completed a run within its \(bot.expectedDays ?? 0)-day window."
                )
                Spacer()
                Button("Open Bot") { state.openBot(bot.id) }
              }
              .card()
            }
            ForEach(bots.home.needsYou) { FindingCard(finding: $0) }
            if bots.home.isCalm {
              Text("No decisions, blockers, or failures waiting for you.")
                .foregroundStyle(Theme.muted)
            }
          }
          VStack(alignment: .leading, spacing: 12) {
            Eyebrow("Material developments")
            ForEach(bots.home.material) { FindingCard(finding: $0) }
            if bots.home.material.isEmpty {
              Text("Nothing new to report.").foregroundStyle(Theme.muted)
            }
          }
          VStack(alignment: .leading, spacing: 12) {
            HStack {
              Eyebrow("Your bots")
              Spacer()
              Button("Manage Bots") { state.selection = .bots }
            }
            ForEach(bots.home.bots) { bot in
              Button {
                state.openBot(bot.id)
              } label: {
                BotHealthRow(bot: bot)
              }
              .buttonStyle(.plain)
              Divider()
            }
            if bots.home.bots.isEmpty {
              Text("Give a specialist an ongoing responsibility—without starting a conversation.")
                .foregroundStyle(Theme.muted)
              Button("Create Your First Bot") { state.selection = .bots }
                .buttonStyle(.primary)
            }
          }
          HStack {
            Text("Need something now?").foregroundStyle(Theme.muted)
            Button("Talk to the Default Bot") { state.newChat() }
              .disabled(chats.offline)
          }
        }
      }
      .frame(maxWidth: 850, alignment: .leading)
      .padding(30)
      .frame(maxWidth: .infinity)
    }
    .navigationTitle("Home")
    .toolbar {
      Button("Refresh", systemImage: "arrow.clockwise") {
        Task {
          do { try await bots.refresh() } catch {
            bots.error = "Could not refresh Home. Reconnect and try again."
          }
        }
      }
      .disabled(chats.offline || bots.working)
    }
    .safeAreaInset(edge: .top, spacing: 0) { BotErrorBanner() }
  }
}

struct BotErrorBanner: View {
  @Environment(BotController.self) private var bots

  var body: some View {
    if let error = bots.error {
      Banner(text: error, tint: Theme.danger) {
        Button("Dismiss", systemImage: "xmark") { bots.error = nil }
          .labelStyle(.iconOnly).buttonStyle(.borderless)
      }
    }
  }
}

func botOutcome(_ value: String) -> String {
  [
    "quiet": "Quiet", "updated": "Updated", "needs-attention": "Needs attention",
    "blocked": "Blocked", "failed": "Failed", "incomplete": "Incomplete",
    "running": "Running", "complete": "Complete", "missed": "Missed",
    "stopped": "Stopped", "interrupted": "Interrupted",
  ][value] ?? value
}

struct BotHealthRow: View {
  let bot: Bot

  var body: some View {
    HStack(alignment: .firstTextBaseline) {
      Image(systemName: bot.enabled ? "sparkles" : "pause.circle")
        .foregroundStyle(bot.overdue ? Theme.danger : Theme.accent)
      Text(bot.name).fontWeight(.medium)
      if let last = bot.lastRun {
        Text(botOutcome(last.outcome ?? last.status))
          .foregroundStyle(Theme.muted)
      } else {
        Text("Not checked yet").foregroundStyle(Theme.muted)
      }
      Spacer()
      Text(bot.enabled ? (bot.nextRun.map { "Next \(when($0))" } ?? "No next run") : "Paused")
        .font(.caption).foregroundStyle(Theme.muted)
    }
    .padding(.vertical, 5)
    .accessibilityElement(children: .combine)
  }
}

/// The unit of attention: a sourced finding, with acknowledgement, snooze,
/// dismissal, feedback, and a link to its evidence. It never executes actions.
struct FindingCard: View {
  @Environment(AppState.self) private var state
  @Environment(BotController.self) private var bots
  @Environment(ChatController.self) private var chats
  let finding: Finding
  @State private var expanded = false

  var body: some View {
    VStack(alignment: .leading, spacing: 10) {
      HStack(alignment: .firstTextBaseline) {
        Text(finding.botName).font(.caption).foregroundStyle(Theme.accent)
        if finding.held { Text("Held · uncertain").font(.caption) }
        if finding.state == "seen" { Text("Seen").font(.caption).foregroundStyle(Theme.muted) }
        Spacer()
        Text(when(finding.lastSeen)).font(.caption).foregroundStyle(Theme.muted)
      }
      Text(finding.title).font(Theme.display(18, relativeTo: .headline))
        .textSelection(.enabled)
      if !finding.detail.isEmpty {
        Text(finding.detail).lineLimit(expanded ? nil : 4).textSelection(.enabled)
        Button(expanded ? "Less" : "More") { expanded.toggle() }
          .buttonStyle(.link).controlSize(.small)
      }
      if !finding.sources.isEmpty {
        HStack {
          ForEach(Array(finding.sources.enumerated()), id: \.offset) { _, source in
            if let url = URL(string: source), ["https", "http"].contains(url.scheme ?? "") {
              Link(host(source).isEmpty ? "Source" : host(source), destination: url)
                .environment(\.openURL, webLinks)
            }
          }
        }
        .font(.caption)
      }
      if finding.kind == "approval" {
        Text("This is a proposal, not permission to act. Discuss it before taking action.")
          .font(.caption).foregroundStyle(Theme.muted)
      }
      HStack {
        if finding.state == "new" {
          Button("Seen") { set("seen") }
        }
        Button("Resolve") { set("resolved") }
        Menu("Snooze") {
          Button("For a Day") { snooze(days: 1) }
          Button("For a Week") { snooze(days: 7) }
          Button("Until It Changes") { set("snoozed") }
        }
        Menu("Feedback") {
          Button("Not Material · Dismiss") {
            Task { await bots.feedback(finding.id, verdict: "not-material") }
          }
          Button("More Like This") {
            Task { await bots.feedback(finding.id, verdict: "material") }
          }
          Divider()
          Button("Dismiss Without Teaching") { set("dismissed") }
        }
        Spacer()
        if let runId = finding.runId {
          Button("Evidence") { state.openBotRun(finding.taskId, runId) }
        }
        Button("Open Bot") { state.openBot(finding.taskId) }
      }
      .controlSize(.small)
      .disabled(bots.working || chats.offline)
    }
    .card(highlighted: finding.severity == "attention" && finding.state == "new")
  }

  private func set(_ value: String) {
    Task { await bots.setFinding(finding.id, state: value) }
  }

  private func snooze(days: Int) {
    let until = Date.now.addingTimeInterval(Double(days) * 86400)
    Task { await bots.setFinding(finding.id, state: "snoozed", until: until) }
  }
}
