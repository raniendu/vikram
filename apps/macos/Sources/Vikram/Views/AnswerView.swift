import SwiftUI
import VikramKit

/// One of Vikram's replies: thinking, tool steps, the answer, and its evidence.
struct AnswerView: View {
    let attempt: Attempt
    var respond: Respond?
    var handBack: (() -> Void)?
    var openTask: ((String) -> Void)?

    private var hasChips: Bool {
        attempt.reasoning?.isEmpty == false || !(attempt.searches ?? []).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            byline
            if hasChips {
                HStack(alignment: .top, spacing: 6) {
                    ThinkingChip(attempt: attempt)
                    WebActivityChip(searches: attempt.searches ?? [])
                }
            }
            if attempt.activity?.isEmpty == false {
                ActivityList(
                    attempt: attempt, respond: respond, handBack: handBack, openTask: openTask)
            }
            if !attempt.content.isEmpty {
                MarkdownView(attempt.content)
            }
            if attempt.status == "running"
                && !(attempt.phase == "thinking" && attempt.reasoning?.isEmpty == false)
            {
                HStack(spacing: 8) {
                    ActivityDot()
                    Text(phaseText).foregroundStyle(Theme.muted)
                }
                .font(.callout)
                .accessibilityElement(children: .combine)
            }
            if (attempt.searches ?? []).contains(where: { $0.status == "failed" }) {
                Text(
                    "Web search was unavailable or rate limited. Some current information could not be verified."
                )
                .font(.callout).foregroundStyle(Theme.muted)
            }
            if !(attempt.sources ?? []).isEmpty { SourceList(sources: attempt.sources ?? []) }
            if attempt.status != "running" && attempt.status != "complete" {
                VStack(alignment: .leading, spacing: 4) {
                    (Text(attempt.status.capitalized).bold()
                        + Text(" · This partial reply is excluded from future context."))
                    if let error = attempt.error { Text(error) }
                }
                .font(.callout)
                .foregroundStyle(Theme.muted)
                .textSelection(.enabled)
            }
            if attempt.unsaved == true {
                Label(
                    "Unsaved reply. Copy this text before closing or reconnecting.",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.callout)
                .foregroundStyle(Theme.danger)
            }
            if !attempt.content.isEmpty {
                CopyButton(text: attempt.content, label: "Copy reply")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var byline: some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            BrandMark()
            Text("Vikram").font(.callout.weight(.semibold))
            Text(
                formatModelName(attempt.actualModel ?? attempt.requestedModel)
                    + (attempt.actualModel == nil ? " · requested" : "")
            )
            .font(.caption)
            .foregroundStyle(Theme.muted)
            .help(attempt.actualModel ?? attempt.requestedModel)
        }
        .accessibilityElement(children: .combine)
    }

    private var phaseText: String {
        switch attempt.phase {
        case "searching": "Searching the web…"
        case "fetching": "Reading sources…"
        case "sandbox": "Running code in the sandbox…"
        case "asking": "Waiting for your answer…"
        case "reviewing": "Waiting for your approval…"
        case "browsing": "Using the browser…"
        case "approving": "Waiting for your OK…"
        case "paused": "Waiting for you to hand back the browser…"
        default:
            !attempt.content.isEmpty
                ? "Writing…"
                : attempt.phase == "thinking"
                    ? "Thinking…" : attempt.phase == "loading" ? "Waiting for Ollama…" : "Preparing reply…"
        }
    }
}

/// The model's thinking, collapsed until the person opens it.
private struct ThinkingChip: View {
    let attempt: Attempt
    @State private var open = false

    var body: some View {
        if let reasoning = attempt.reasoning, !reasoning.isEmpty {
            let active = attempt.status == "running" && attempt.phase == "thinking"
            ChipDisclosure(isExpanded: $open) {
                HStack(spacing: 5) {
                    if active { ActivityDot() }
                    Text(active ? "Thinking…" : "Thinking")
                }
            } content: {
                VStack(alignment: .leading, spacing: 6) {
                    ScrollViewReader { proxy in
                        ScrollView {
                            Text(reasoning)
                                .font(.callout)
                                .foregroundStyle(Theme.muted)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            Color.clear.frame(height: 1).id("end")
                        }
                        .frame(maxHeight: 240)
                        .onChange(of: reasoning) { if active { proxy.scrollTo("end") } }
                    }
                    .accessibilityLabel("Model thinking")
                    if attempt.reasoningTruncated == true {
                        Text("Thinking reached the display limit; the answer is unaffected.")
                            .font(.caption).foregroundStyle(Theme.muted)
                    }
                    CopyButton(text: reasoning, label: "Copy thinking")
                }
                .padding(12)
                .frame(minWidth: 320, maxWidth: 640, alignment: .leading)
                .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10))
            }
        }
    }
}

private struct WebActivityChip: View {
    let searches: [Search]
    @State private var open = false

    var body: some View {
        if !searches.isEmpty {
            ChipDisclosure(isExpanded: $open) {
                Text(
                    "Web activity · \(searches.count) \(searches.count == 1 ? "request" : "requests")")
            } content: {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(searches.enumerated()), id: \.offset) { _, search in
                        VStack(alignment: .leading, spacing: 2) {
                            Eyebrow("\(search.tool == "web_search" ? "Search" : "Read") · \(search.status)")
                            Text(
                                (search.arguments.searchQueries ?? search.arguments.urls ?? [])
                                    .joined(separator: " · ")
                            )
                            if let error = search.error {
                                Text(error).foregroundStyle(Theme.muted)
                            }
                        }
                    }
                }
                .font(.callout)
                .textSelection(.enabled)
                .padding(12)
                .frame(minWidth: 280, maxWidth: 560, alignment: .leading)
                .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10))
            }
        }
    }
}

private struct SourceList: View {
    let sources: [Source]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Eyebrow("Retrieved sources")
            ForEach(sources) { source in
                Button {
                    if let url = URL(string: source.url) { openWeb(url) }
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(source.id).font(.caption.monospacedDigit()).foregroundStyle(Theme.muted)
                        Text(source.title.isEmpty ? source.url : source.title)
                            .foregroundStyle(Theme.link)
                            .lineLimit(1)
                    }
                    .font(.callout)
                }
                .buttonStyle(.plain)
                .help("\(source.url) · retrieved \(source.retrievedAt ?? "")")
            }
        }
    }
}

/// A chat's messages, newest at the bottom, following a running reply while
/// the person stays at the end.
struct TranscriptView: View {
    let chat: Chat
    let respond: Respond
    let handBack: () -> Void
    let openTask: (String) -> Void
    let canRetry: Bool
    let retry: () -> Void
    @State private var bottom: String?
    @State private var following = true

    private var lastAttempt: Attempt? { chat.messages.last?.attempts.last }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 30) {
                    ForEach(Array(chat.messages.enumerated()), id: \.element.id) { index, message in
                        TurnView(
                            chat: chat, message: message, isFirst: index == 0,
                            isLast: index == chat.messages.count - 1,
                            respond: respond, handBack: handBack, openTask: openTask,
                            canRetry: canRetry, retry: retry)
                    }
                    Color.clear.frame(height: 1).id("end")
                }
                .scrollTargetLayout()
                .frame(maxWidth: 700)
                .padding(.horizontal, 28)
                .padding(.vertical, 24)
                .frame(maxWidth: .infinity)
            }
            .scrollPosition(id: $bottom, anchor: .bottom)
            .onChange(of: bottom) { following = bottom == "end" || bottom == nil }
            .onChange(of: chat.id, initial: true) {
                following = true
                proxy.scrollTo("end", anchor: .bottom)
            }
            .onChange(of: lastAttempt) {
                if following { proxy.scrollTo("end", anchor: .bottom) }
            }
            .onChange(of: chat.messages.count) {
                following = true
                proxy.scrollTo("end", anchor: .bottom)
            }
            .overlay(alignment: .bottom) {
                if !following {
                    Button("Latest", systemImage: "arrow.down") {
                        following = true
                        withAnimation { proxy.scrollTo("end", anchor: .bottom) }
                    }
                    .buttonStyle(.secondary)
                    .background(Theme.raised, in: Capsule())
                    .padding(8)
                }
            }
        }
    }
}

private struct TurnView: View {
    let chat: Chat
    let message: Message
    let isFirst: Bool
    let isLast: Bool
    let respond: Respond
    let handBack: () -> Void
    let openTask: (String) -> Void
    let canRetry: Bool
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            if !isFirst { Rectangle().fill(Theme.rule).frame(height: 1) }
            UserMessage(
                text: message.text,
                attachments: chat.attachments.filter { $0.messageId == message.id })
            ForEach(Array(message.attempts.enumerated()), id: \.element.id) { index, attempt in
                if index < message.attempts.count - 1 {
                    PreviousAttempt(attempt: attempt, openTask: openTask)
                } else {
                    AnswerView(
                        attempt: attempt,
                        respond: { record, reply in await respond(record, reply) },
                        handBack: handBack, openTask: openTask)
                }
            }
            if isLast && !message.attempts.isEmpty
                && !message.attempts.contains(where: { ["running", "complete"].contains($0.status) })
            {
                Button("Retry with Selected Model", systemImage: "arrow.counterclockwise", action: retry)
                    .buttonStyle(.secondary)
                    .disabled(!canRetry)
            }
        }
        .accessibilityElement(children: .contain)
    }
}

private struct PreviousAttempt: View {
    let attempt: Attempt
    let openTask: (String) -> Void
    @State private var open = false

    var body: some View {
        ChipDisclosure(isExpanded: $open) {
            Text("Previous attempt · \(attempt.status)")
        } content: {
            AnswerView(attempt: attempt, openTask: openTask)
                .padding(.leading, 12)
                .overlay(alignment: .leading) { Rectangle().fill(Theme.rule).frame(width: 2) }
        }
    }
}

/// What the person wrote, in the display face under a small label.
struct UserMessage: View {
    let text: String
    let attachments: [Attachment]
    var label = "You"

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Eyebrow(label)
            if !text.isEmpty {
                Text(text)
                    .font(Theme.display(19, weight: .regular, relativeTo: .title3))
                    .lineSpacing(4)
                    .foregroundStyle(Theme.ink)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !attachments.isEmpty {
                AttachmentStrip(attachments: attachments)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(label): \(text)")
    }
}
