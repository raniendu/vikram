import Foundation

// How the window keeps a reply's live state consistent with what the service
// saved: events can arrive late, a send acknowledgement can be lost, and the
// service can restart while a reply is running.

/// Thinking is on unless explicitly off: `true`, else the model's own level.
public func thinkingOn(_ model: LocalModel) -> Thinking? {
    if model.thinking.contains(.bool(true)) { return .bool(true) }
    if case .level(let level) = model.thinkingDefault, model.thinking.contains(.level(level)) {
        return .level(level)
    }
    return model.thinking.first { if case .level = $0 { true } else { false } }
}

/// The chat with one runtime event applied to its matching attempt.
public func applyAttempt(_ chat: Chat, _ event: RuntimeEvent) -> Chat {
    guard event.chatId == chat.id, let attemptId = event.attemptId,
        let requestId = event.requestId
    else { return chat }
    var chat = chat
    for m in chat.messages.indices {
        for a in chat.messages[m].attempts.indices {
            let attempt = chat.messages[m].attempts[a]
            guard attempt.id == attemptId, attempt.requestId == requestId else { continue }
            // A terminal state never accepts a delayed running event.
            if attempt.status != "running" && event.status == "running" { continue }
            if (event.revision ?? 0) < (attempt.revision ?? 0) { continue }
            chat.messages[m].attempts[a] = merged(attempt, event)
        }
    }
    return chat
}

private func merged(_ a: Attempt, _ event: RuntimeEvent) -> Attempt {
    var a = a
    a.content = event.content ?? a.content
    a.reasoning = event.reasoning ?? a.reasoning
    a.reasoningTruncated = event.reasoningTruncated ?? a.reasoningTruncated
    a.actualModel = event.actualModel ?? a.actualModel
    a.status = event.status ?? a.status
    a.error = event.error ?? a.error
    a.unsaved = event.unsaved ?? a.unsaved
    a.phase = event.phase ?? a.phase
    a.sources = event.sources ?? a.sources
    a.searches = event.searches ?? a.searches
    a.activity = event.activity ?? a.activity
    a.revision = event.revision ?? a.revision
    return a
}

/// The service's active reply, unless this window already saw it end.
public func reconcileActive(_ active: Active?, _ events: [String: RuntimeEvent]) -> Active? {
    guard let active else { return nil }
    if let event = events[active.attemptId], event.requestId == active.requestId,
        let status = event.status, status != "running"
    {
        return nil
    }
    return active
}

/// A saved chat, with any newer output this window holds that could not be saved.
public func recoverChat(_ chat: Chat, _ events: [String: RuntimeEvent]) -> Chat {
    var chat = chat
    for m in chat.messages.indices {
        for a in chat.messages[m].attempts.indices {
            let attempt = chat.messages[m].attempts[a]
            // Tool calls can replace prior text. Only checkpoint order establishes freshness.
            guard let event = events[attempt.id], event.requestId == attempt.requestId,
                event.unsaved == true, (event.revision ?? 0) > (attempt.revision ?? 0)
            else { continue }
            var recovered = merged(attempt, event)
            recovered.revision = event.revision
            recovered.status = "interrupted"
            recovered.unsaved = true
            recovered.phase = attempt.phase
            recovered.error =
                event.error ?? "Unsaved text recovered from this window. Copy it before closing."
            chat.messages[m].attempts[a] = recovered
        }
    }
    return chat
}

public struct DraftRecord: Sendable, Hashable {
    public var text: String
    public var revision: Int

    public init(text: String, revision: Int) {
        self.text = text
        self.revision = revision
    }
}

/// A sent draft, kept until the service acknowledges the send.
public struct Submission: Sendable, Hashable {
    public var requestId: String
    public var text: String
    public var revision: Int

    public init(requestId: String, text: String, revision: Int) {
        self.requestId = requestId
        self.text = text
        self.revision = revision
    }
}

/// The draft to keep after a send whose outcome is uncertain: the saved one if
/// the send went through and nothing was typed since, else the window's text.
public func recoverDraft(_ chat: Chat, _ draft: DraftRecord, _ submission: Submission?)
    -> DraftRecord
{
    let committed =
        submission.map { submission in
            chat.messages.contains { $0.attempts.contains { $0.requestId == submission.requestId } }
        } ?? false
    let unchanged =
        committed && draft.text == submission?.text && draft.revision == submission?.revision
    return DraftRecord(
        text: unchanged ? chat.summary.draft : draft.text,
        revision: max(chat.summary.draftRevision, draft.revision) + 1)
}
