import SwiftUI
import VikramKit

/// Answers a waiting step of the running reply; true when the service took it.
typealias Respond = (ActivityRecord, Reply) async -> Bool

private let questionStatus = ["declined": "Skipped", "timed_out": "Not answered in time"]
private let planStatus = [
    "approved": "Approved", "changes_requested": "Changes requested",
    "not_approved": "Not approved", "timed_out": "Not reviewed in time",
]
private let browserDone = [
    "open": "Opened", "click": "Clicked", "type": "Typed into", "scroll": "Scrolled",
    "back": "Went back", "look": "Looked at the page",
]
private let browserDoing = [
    "open": "Opening", "click": "Clicking", "type": "Typing into", "scroll": "Scrolling",
    "back": "Going back", "look": "Looking at the page",
]
private let browserStatus = [
    "denied": "not allowed", "refused": "refused", "failed": "failed",
    "timed_out": "timed out", "stopped": "stopped", "interrupted": "interrupted",
    "waiting": "waiting for your OK", "paused": "waiting for the browser",
]
private let taskDone = [
    "create": "Scheduled", "update": "Changed", "pause": "Paused", "resume": "Resumed",
    "delete": "Deleted",
]
private let taskStatus = [
    "denied": "not saved", "timed_out": "not approved in time", "failed": "failed",
    "stopped": "stopped", "interrupted": "interrupted", "running": "saving",
]

func browserStep(_ record: BrowserRecord) -> String {
    let pending = ["running", "waiting", "paused"].contains(record.status)
    var text = (pending ? browserDoing : browserDone)[record.action] ?? "Used the browser"
    if let target = record.target { text += " \(target)" }
    if let typed = record.text { text += ": “\(typed)”" }
    return text
}

/// A reply's tool steps. Consecutive browser actions share one log; steps that
/// wait for the person stand out as cards with controls.
struct ActivityList: View {
    let attempt: Attempt
    var respond: Respond?
    var handBack: (() -> Void)?
    var openTask: ((String) -> Void)?

    private enum Group: Identifiable {
        case single(ActivityRecord)
        case browser([BrowserRecord])

        var id: String {
            switch self {
            case .single(let record): "\(record.kind)-\(record.id)"
            case .browser(let records): "browser-\(records[0].id)"
            }
        }
    }

    private var groups: [Group] {
        var groups: [Group] = []
        for record in attempt.activity ?? [] {
            if case .browser(let step) = record {
                if case .browser(var steps)? = groups.last {
                    steps.append(step)
                    groups[groups.count - 1] = .browser(steps)
                } else {
                    groups.append(.browser([step]))
                }
            } else {
                groups.append(.single(record))
            }
        }
        return groups
    }

    private var running: Bool { attempt.status == "running" && attempt.unsaved != true }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(groups) { group in
                switch group {
                case .single(let record): single(record)
                case .browser(let steps): browser(steps)
                }
            }
        }
    }

    @ViewBuilder private func single(_ record: ActivityRecord) -> some View {
        let live = respond != nil && running && record.status == "waiting"
        let reply: (Reply) async -> Bool = { reply in await respond?(record, reply) ?? false }
        switch record {
        case .question(let question):
            if live { QuestionCard(record: question, reply: reply) } else {
                QuestionSummary(record: question)
            }
        case .plan(let plan):
            if live { PlanCard(record: plan, reply: reply) } else { PlanSummary(record: plan) }
        case .code(let code):
            CodeRun(record: code)
        case .schedule(let task):
            if live { ScheduleApproval(record: task, reply: reply) } else {
                ScheduleSummary(record: task, openTask: openTask)
            }
        case .tool(let tool):
            ToolActivityView(record: tool, live: live, reply: reply)
        case .browser, .other:
            EmptyView()
        }
    }

    @ViewBuilder private func browser(_ steps: [BrowserRecord]) -> some View {
        let waiting = steps.first { running && ($0.status == "waiting" || $0.status == "paused") }
        BrowserLog(steps: steps, running: running)
        if let waiting, waiting.status == "waiting", let respond {
            BrowserApproval(record: waiting) { reply in await respond(.browser(waiting), reply) }
        }
        if waiting?.status == "paused" {
            BrowserPaused(handBack: handBack)
        }
    }
}

// MARK: Questions

private struct QuestionCard: View {
    let record: QuestionRecord
    let reply: (Reply) async -> Bool
    @State private var picked: [String: [String]] = [:]
    @State private var typed: [String: String] = [:]
    @State private var sending = false

    private var ready: Bool {
        record.questions.allSatisfy { question in
            !(typed[question.header] ?? "").trimmingCharacters(in: .whitespaces).isEmpty
                || !(picked[question.header] ?? []).isEmpty
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Vikram needs your answer", systemImage: "questionmark.bubble")
                .font(Theme.display(16, weight: .medium, relativeTo: .headline))
            ForEach(record.questions, id: \.header) { question in
                VStack(alignment: .leading, spacing: 6) {
                    Text(question.question).fontWeight(.medium)
                    ForEach(question.options, id: \.label) { option in
                        optionRow(question, option)
                    }
                    TextField(
                        "Or type your own answer",
                        text: Binding(
                            get: { typed[question.header] ?? "" },
                            set: { value in
                                typed[question.header] = String(value.prefix(2000))
                                if !value.isEmpty { picked[question.header] = [] }
                            })
                    )
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Your own answer to: \(question.question)")
                }
                .accessibilityElement(children: .contain)
            }
            HStack {
                Button("Send Answer") { submit() }
                    .buttonStyle(.primary)
                    .disabled(!ready || sending)
                Button("Skip") {
                    send(.skip)
                }
                .buttonStyle(.secondary)
                .disabled(sending)
            }
        }
        .card(highlighted: true)
        .tint(Theme.accent)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Vikram's question")
    }

    private func optionRow(_ question: Question, _ option: QuestionOption) -> some View {
        let multi = question.multiSelect == true
        let chosen = picked[question.header]?.contains(option.label) ?? false
        return Button {
            var current = picked[question.header] ?? []
            if !multi {
                current = [option.label]
            } else if chosen {
                current.removeAll { $0 == option.label }
            } else {
                current.append(option.label)
            }
            picked[question.header] = current
            typed[question.header] = ""
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(
                    systemName: multi
                        ? (chosen ? "checkmark.square.fill" : "square")
                        : (chosen ? "largecircle.fill.circle" : "circle")
                )
                .foregroundStyle(chosen ? Theme.accent : Theme.muted)
                VStack(alignment: .leading, spacing: 2) {
                    Text(option.label)
                    if let description = option.description, !description.isEmpty {
                        Text(description).font(.caption).foregroundStyle(Theme.muted)
                    }
                }
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(sending)
        .accessibilityAddTraits(chosen ? .isSelected : [])
    }

    private func submit() {
        guard ready else { return }
        send(
            .answers(
                record.questions.map { question in
                    let own = (typed[question.header] ?? "").trimmingCharacters(in: .whitespaces)
                    return own.isEmpty
                        ? QuestionAnswer(header: question.header, selected: picked[question.header] ?? [])
                        : QuestionAnswer(header: question.header, customAnswer: own)
                }))
    }

    private func send(_ answer: Reply) {
        sending = true
        Task {
            _ = await reply(answer)
            sending = false
        }
    }
}

private struct QuestionSummary: View {
    let record: QuestionRecord

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(record.questions, id: \.header) { question in
                let answer = record.answers?[question.header] ?? []
                Label {
                    Text(question.question) + Text("  →  ").foregroundStyle(Theme.muted)
                        + Text(
                            answer.isEmpty
                                ? questionStatus[record.status] ?? "Not answered"
                                : answer.joined(separator: ", ")
                        ).bold()
                } icon: {
                    Image(systemName: "questionmark.bubble")
                }
                .font(.callout)
            }
        }
        .foregroundStyle(Theme.muted)
    }
}

// MARK: Plans

private struct PlanCard: View {
    let record: PlanRecord
    let reply: (Reply) async -> Bool
    @State private var changing = false
    @State private var feedback = ""
    @State private var sending = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Plan for your approval", systemImage: "list.bullet.clipboard")
                .font(Theme.display(16, weight: .medium, relativeTo: .headline))
            MarkdownView(record.plan)
            if changing {
                TextEditor(text: $feedback)
                    .font(.body)
                    .frame(minHeight: 60, maxHeight: 140)
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .background(.background, in: RoundedRectangle(cornerRadius: 6))
                    .overlay { RoundedRectangle(cornerRadius: 6).strokeBorder(.separator) }
                    .accessibilityLabel("Changes to the plan")
                HStack {
                    Button("Send Changes") {
                        send(.revise(feedback: String(feedback.prefix(2000))))
                    }
                    .buttonStyle(.primary)
                    .disabled(
                        feedback.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || sending)
                    Button("Back") { changing = false }.buttonStyle(.secondary).disabled(sending)
                }
            } else {
                HStack {
                    Button("Approve Plan") { send(.approve) }
                        .buttonStyle(.primary)
                    Button("Request Changes…") { changing = true }.buttonStyle(.secondary)
                    Button("Not Now") { send(.dismiss) }.buttonStyle(.secondary)
                }
                .disabled(sending)
            }
        }
        .card(highlighted: true)
        .tint(Theme.accent)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Plan for your approval")
    }

    private func send(_ answer: Reply) {
        sending = true
        Task {
            _ = await reply(answer)
            sending = false
        }
    }
}

private struct PlanSummary: View {
    let record: PlanRecord
    @State private var open = false

    var body: some View {
        ChipDisclosure(isExpanded: $open) {
            Text("Plan · \(planStatus[record.status] ?? "Not reviewed")")
        } content: {
            VStack(alignment: .leading, spacing: 8) {
                MarkdownView(record.plan)
                if let feedback = record.feedback {
                    Text("Your changes: \(feedback)").font(.callout).foregroundStyle(Theme.muted)
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10))
        }
    }
}

// MARK: Sandbox

private struct CodeRun: View {
    let record: CodeRecord
    @State private var open = false

    var body: some View {
        let running = record.status == "running"
        ChipDisclosure(isExpanded: $open) {
            HStack(spacing: 5) {
                if running { ActivityDot() }
                Text(
                    running
                        ? "Running Python in the sandbox…"
                        : "Ran Python · \(record.status == "complete" ? "Complete" : record.status == "failed" ? "Failed" : "Stopped")"
                )
            }
        } content: {
            VStack(alignment: .leading, spacing: 8) {
                CodeBox(title: "python · sandbox", text: record.code)
                if !running {
                    let result = record.error ?? record.output
                    CodeBox(
                        title: record.error != nil ? "error" : "output",
                        text: result.isEmpty ? "(no output)" : result, copyLabel: nil)
                }
            }
        }
    }
}

// MARK: Browser

private struct BrowserLog: View {
    let steps: [BrowserRecord]
    let running: Bool
    @State private var expanded: Bool?

    var body: some View {
        let last = steps.last!
        let place = host(steps.last { $0.url != nil }?.url)
        ChipDisclosure(
            isExpanded: Binding(get: { expanded ?? running }, set: { expanded = $0 })
        ) {
            HStack(spacing: 5) {
                if running && last.status == "running" { ActivityDot() }
                Text(
                    "Browser · \(steps.count) \(steps.count == 1 ? "action" : "actions")"
                        + (place.isEmpty ? "" : " · \(place)"))
            }
        } content: {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(Array(steps.enumerated()), id: \.element.id) { index, step in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("\(index + 1).").foregroundStyle(Theme.muted).monospacedDigit()
                        VStack(alignment: .leading, spacing: 2) {
                            (Text(browserStep(step))
                                + (["complete", "running"].contains(step.status)
                                    ? Text("")
                                    : Text(" · \(browserStatus[step.status] ?? step.status)").bold()))
                            if let error = step.error {
                                Text(error).font(.caption).foregroundStyle(Theme.muted)
                            }
                        }
                    }
                }
            }
            .font(.callout)
            .textSelection(.enabled)
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10))
        }
    }
}

private struct BrowserApproval: View {
    let record: BrowserRecord
    let reply: (Reply) async -> Bool
    @State private var sending = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Allow this browser action?", systemImage: "exclamationmark.shield")
                .font(Theme.display(16, weight: .medium, relativeTo: .headline))
            Text(record.approval?.summary ?? browserStep(record)).fontWeight(.medium)
            let place = host(record.url)
            Text((record.approval?.reason ?? "") + (place.isEmpty ? "" : " · \(place)"))
                .font(.callout).foregroundStyle(Theme.muted)
            HStack {
                Button("Allow") { send(.approve) }.buttonStyle(.primary)
                Button("Don’t Allow") { send(.dismiss) }.buttonStyle(.secondary)
            }
            .disabled(sending)
        }
        .card(highlighted: true)
        .tint(Theme.accent)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Browser action for your approval")
    }

    private func send(_ answer: Reply) {
        sending = true
        Task {
            _ = await reply(answer)
            sending = false
        }
    }
}

private struct BrowserPaused: View {
    let handBack: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Vikram is waiting for the browser", systemImage: "globe").font(Theme.display(16, weight: .medium, relativeTo: .headline))
            Text("You took the browser over. Vikram continues when you hand it back.")
                .font(.callout).foregroundStyle(Theme.muted)
            if let handBack {
                Button("Hand Back", systemImage: "arrow.uturn.backward", action: handBack)
                    .buttonStyle(.primary)
            }
        }
        .card(highlighted: true)
        .tint(Theme.accent)
        .accessibilityElement(children: .contain)
    }
}

// MARK: Scheduled tasks

private struct ScheduleApproval: View {
    let record: ScheduleRecord
    let reply: (Reply) async -> Bool
    @State private var sending = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(
                record.action == "create"
                    ? "Save this scheduled task?" : "Save these changes to the task?",
                systemImage: "calendar.badge.clock"
            )
            .font(Theme.display(16, weight: .medium, relativeTo: .headline))
            Text("\(record.name ?? "Task") · \(record.description ?? "")").fontWeight(.medium)
            if let prompt = record.prompt {
                Text(prompt).font(.callout).textSelection(.enabled)
            }
            Text("Vikram will run this on its own, even when the app is closed.")
                .font(.callout).foregroundStyle(Theme.muted)
            HStack {
                Button("Save Task") { send(.approve) }.buttonStyle(.primary)
                Button("Don’t Save") { send(.dismiss) }.buttonStyle(.secondary)
            }
            .disabled(sending)
        }
        .card(highlighted: true)
        .tint(Theme.accent)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Scheduled task for your approval")
    }

    private func send(_ answer: Reply) {
        sending = true
        Task {
            _ = await reply(answer)
            sending = false
        }
    }
}

private struct ScheduleSummary: View {
    let record: ScheduleRecord
    let openTask: ((String) -> Void)?

    var body: some View {
        let done = record.status == "complete"
        let label =
            done
            ? taskDone[record.action] ?? "Changed"
            : record.action == "create" ? "Scheduled task" : taskDone[record.action] ?? "Task"
        HStack(alignment: .firstTextBaseline) {
            Label {
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(label) “\(record.name ?? "task")”")
                        + Text(
                            (["create", "update"].contains(record.action)
                                ? record.description.map { " · \($0)" } : nil) ?? "")
                        + (done
                            ? Text("") : Text(" · \(taskStatus[record.status] ?? record.status)").bold())
                    if let error = record.error, !done {
                        Text(error).font(.caption).foregroundStyle(Theme.muted)
                    }
                }
            } icon: {
                Image(systemName: "calendar.badge.clock")
            }
            if done, let taskId = record.taskId, record.action != "delete", let openTask {
                Button("Open") { openTask(taskId) }.buttonStyle(.link)
            }
        }
        .font(.callout)
    }
}
