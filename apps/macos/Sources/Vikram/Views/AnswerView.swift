import SwiftUI
import VikramKit

/// One of Vikram's replies: thinking, tool steps, the answer, and its evidence.
struct AnswerView: View {
    let attempt: Attempt
    var respond: Respond?
    var handBack: (() -> Void)?
    var openTask: ((String) -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            byline
            ThinkingDisclosure(attempt: attempt)
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
                    Text(phaseText).foregroundStyle(.secondary)
                }
                .font(.callout)
                .accessibilityElement(children: .combine)
            }
            WebActivity(attempt: attempt)
            if !(attempt.sources ?? []).isEmpty { SourceList(sources: attempt.sources ?? []) }
            if attempt.status != "running" && attempt.status != "complete" {
                VStack(alignment: .leading, spacing: 4) {
                    (Text(attempt.status.capitalized).bold()
                        + Text(" · This partial reply is excluded from future context."))
                    if let error = attempt.error { Text(error) }
                }
                .font(.callout)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            }
            if attempt.unsaved == true {
                Label(
                    "Unsaved reply. Copy this text before closing or reconnecting.",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.callout)
                .foregroundStyle(.orange)
            }
            if !attempt.content.isEmpty {
                CopyButton(text: attempt.content, label: "Copy reply")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var byline: some View {
        HStack(spacing: 6) {
            Image(systemName: "sparkle").foregroundStyle(.tint)
            Text("Vikram").fontWeight(.semibold)
            Text(
                formatModelName(attempt.actualModel ?? attempt.requestedModel)
                    + (attempt.actualModel == nil ? " · requested" : "")
            )
            .foregroundStyle(.secondary)
            .help(attempt.actualModel ?? attempt.requestedModel)
        }
        .font(.callout)
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
private struct ThinkingDisclosure: View {
    let attempt: Attempt
    @State private var open = false

    var body: some View {
        if let reasoning = attempt.reasoning, !reasoning.isEmpty {
            let active = attempt.status == "running" && attempt.phase == "thinking"
            DisclosureGroup(isExpanded: $open) {
                VStack(alignment: .leading, spacing: 6) {
                    ScrollViewReader { proxy in
                        ScrollView {
                            Text(reasoning)
                                .font(.callout)
                                .foregroundStyle(.secondary)
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
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    CopyButton(text: reasoning, label: "Copy thinking")
                }
                .padding(.top, 4)
            } label: {
                HStack(spacing: 6) {
                    if active { ActivityDot() }
                    Text(active ? "Thinking…" : "Thinking")
                }
                .foregroundStyle(.secondary)
            }
            .font(.callout)
        }
    }
}

private struct WebActivity: View {
    let attempt: Attempt

    var body: some View {
        let searches = attempt.searches ?? []
        if !searches.isEmpty {
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(searches.enumerated()), id: \.offset) { _, search in
                        VStack(alignment: .leading, spacing: 2) {
                            Text("\(search.tool == "web_search" ? "Search" : "Read") · \(search.status)")
                                .fontWeight(.medium)
                            Text(
                                (search.arguments.searchQueries ?? search.arguments.urls ?? [])
                                    .joined(separator: " · ")
                            )
                            .foregroundStyle(.secondary)
                            if let error = search.error { Text(error).foregroundStyle(.secondary) }
                        }
                    }
                }
                .textSelection(.enabled)
                .padding(.top, 4)
            } label: {
                Label(
                    "Web activity · \(searches.count) \(searches.count == 1 ? "request" : "requests")",
                    systemImage: "magnifyingglass")
            }
            .font(.callout)
            if searches.contains(where: { $0.status == "failed" }) {
                Text(
                    "Web search was unavailable or rate limited. Some current information could not be verified."
                )
                .font(.callout).foregroundStyle(.secondary)
            }
        }
    }
}

private struct SourceList: View {
    let sources: [Source]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Retrieved sources").font(.caption).foregroundStyle(.secondary)
                .textCase(.uppercase)
            ForEach(sources) { source in
                Button {
                    if let url = URL(string: source.url) { openWeb(url) }
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(source.id).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        Text(source.title.isEmpty ? source.url : source.title)
                            .lineLimit(1)
                    }
                }
                .buttonStyle(.link)
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
                LazyVStack(alignment: .leading, spacing: 28) {
                    ForEach(Array(chat.messages.enumerated()), id: \.element.id) { index, message in
                        TurnView(
                            chat: chat, message: message, isLast: index == chat.messages.count - 1,
                            respond: respond, handBack: handBack, openTask: openTask,
                            canRetry: canRetry, retry: retry)
                    }
                    Color.clear.frame(height: 1).id("end")
                }
                .scrollTargetLayout()
                .frame(maxWidth: 760)
                .padding(.horizontal, 24)
                .padding(.vertical, 20)
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
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .padding(8)
                }
            }
        }
    }
}

private struct TurnView: View {
    let chat: Chat
    let message: Message
    let isLast: Bool
    let respond: Respond
    let handBack: () -> Void
    let openTask: (String) -> Void
    let canRetry: Bool
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            UserMessage(
                text: message.text,
                attachments: chat.attachments.filter { $0.messageId == message.id })
            ForEach(Array(message.attempts.enumerated()), id: \.element.id) { index, attempt in
                if index < message.attempts.count - 1 {
                    DisclosureGroup("Previous attempt · \(attempt.status)") {
                        AnswerView(attempt: attempt, openTask: openTask).padding(.top, 6)
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
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
                    .disabled(!canRetry)
            }
        }
        .accessibilityElement(children: .contain)
    }
}

struct UserMessage: View {
    let text: String
    let attachments: [Attachment]
    var label = "You"

    var body: some View {
        VStack(alignment: .trailing, spacing: 8) {
            if !attachments.isEmpty {
                AttachmentStrip(attachments: attachments)
            }
            if !text.isEmpty {
                Text(text)
                    .textSelection(.enabled)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 9)
                    .background(.tint.opacity(0.14), in: RoundedRectangle(cornerRadius: 14))
                    .accessibilityLabel("\(label): \(text)")
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 60)
    }
}
