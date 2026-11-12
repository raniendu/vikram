import Foundation

// The chat service's records, as `vikram/desktop/store.py` and `runtime.py`
// send them. Field names follow the protocol; Swift names are camel case.

/// A thinking setting: a model's `true`/`false` control or a named level.
public enum Thinking: Sendable, Hashable, Codable {
    case bool(Bool)
    case level(String)

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else {
            self = .level(try container.decode(String.self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .bool(let value): try container.encode(value)
        case .level(let value): try container.encode(value)
        }
    }

    public var json: JSONValue {
        switch self {
        case .bool(let value): .bool(value)
        case .level(let value): .string(value)
        }
    }

    public var label: String {
        switch self {
        case .bool(let value): value ? "On" : "Off"
        case .level(let value): value
        }
    }
}

public enum ChatMode: String, Sendable, Codable {
    case normal, plan
}

public struct QuestionOption: Sendable, Hashable, Codable {
    public var label: String
    public var description: String?
}

public struct Question: Sendable, Hashable, Codable {
    public var header: String
    public var question: String
    public var options: [QuestionOption]
    public var multiSelect: Bool?

    enum CodingKeys: String, CodingKey {
        case header, question, options
        case multiSelect = "multi_select"
    }
}

public struct QuestionRecord: Sendable, Hashable, Codable {
    public var id: String
    public var status: String
    public var questions: [Question]
    public var answers: [String: [String]]?
}

public struct PlanRecord: Sendable, Hashable, Codable {
    public var id: String
    public var status: String
    public var plan: String
    public var feedback: String?
}

public struct CodeRecord: Sendable, Hashable, Codable {
    public var id: String
    public var status: String
    public var code: String
    public var output: String
    public var error: String?
}

public struct BrowserApprovalInfo: Sendable, Hashable, Codable {
    public var details: String?
    public var summary: String
    public var reason: String
}

public struct BrowserRecord: Sendable, Hashable, Codable {
    public var id: String
    /// open, click, type, scroll, back, or look.
    public var action: String
    public var status: String
    public var target: String?
    public var text: String?
    public var url: String?
    public var title: String?
    public var error: String?
    public var approval: BrowserApprovalInfo?
}

/// A scheduled task the model created, changed, or deleted in a reply.
public struct ScheduleRecord: Sendable, Hashable, Codable {
    public var id: String
    /// create, update, pause, resume, or delete.
    public var action: String
    public var status: String
    public var taskId: String?
    public var name: String?
    public var description: String?
    public var prompt: String?
    public var nextRun: Double?
    public var error: String?

    enum CodingKeys: String, CodingKey {
        case id, action, status, name, description, prompt, error
        case taskId = "task_id"
        case nextRun = "next_run"
    }
}

/// One tool step in a reply: a question, plan, sandbox run, browser action, or
/// scheduled-task change.
public enum ActivityRecord: Sendable, Hashable, Codable, Identifiable {
    case question(QuestionRecord)
    case plan(PlanRecord)
    case code(CodeRecord)
    case browser(BrowserRecord)
    case schedule(ScheduleRecord)
    case tool(ToolRecord)
    /// A kind this app does not know yet; kept so the reply still shows.
    case other(kind: String, id: String, status: String)

    enum Keys: String, CodingKey { case kind, id, status }

    public init(from decoder: Decoder) throws {
        let keys = try decoder.container(keyedBy: Keys.self)
        let kind = try keys.decode(String.self, forKey: .kind)
        switch kind {
        case "question": self = .question(try QuestionRecord(from: decoder))
        case "plan": self = .plan(try PlanRecord(from: decoder))
        case "code": self = .code(try CodeRecord(from: decoder))
        case "browser": self = .browser(try BrowserRecord(from: decoder))
        case "schedule": self = .schedule(try ScheduleRecord(from: decoder))
        case "coding", "mcp", "extension", "composition": self = .tool(try ToolRecord(from: decoder))
        default:
            self = .other(
                kind: kind,
                id: (try? keys.decode(String.self, forKey: .id)) ?? UUID().uuidString,
                status: (try? keys.decode(String.self, forKey: .status)) ?? "")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var keys = encoder.container(keyedBy: Keys.self)
        try keys.encode(kind, forKey: .kind)
        switch self {
        case .question(let record): try record.encode(to: encoder)
        case .plan(let record): try record.encode(to: encoder)
        case .code(let record): try record.encode(to: encoder)
        case .browser(let record): try record.encode(to: encoder)
        case .schedule(let record): try record.encode(to: encoder)
        case .tool(let record): try record.encode(to: encoder)
        case .other(_, let id, let status):
            try keys.encode(id, forKey: .id)
            try keys.encode(status, forKey: .status)
        }
    }

    public var kind: String {
        switch self {
        case .question: "question"
        case .plan: "plan"
        case .code: "code"
        case .browser: "browser"
        case .schedule: "schedule"
        case .tool(let record): record.kind
        case .other(let kind, _, _): kind
        }
    }

    public var id: String {
        switch self {
        case .question(let r): r.id
        case .plan(let r): r.id
        case .code(let r): r.id
        case .browser(let r): r.id
        case .schedule(let r): r.id
        case .tool(let r): r.id
        case .other(_, let id, _): id
        }
    }

    public var status: String {
        switch self {
        case .question(let r): r.status
        case .plan(let r): r.status
        case .code(let r): r.status
        case .browser(let r): r.status
        case .schedule(let r): r.status
        case .tool(let r): r.status
        case .other(_, _, let status): status
        }
    }
}

public struct Source: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var title: String
    public var url: String
    public var excerpt: String?
    public var retrievedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, title, url, excerpt
        case retrievedAt = "retrieved_at"
    }
}

public struct SearchArguments: Sendable, Hashable, Codable {
    public var searchQueries: [String]?
    public var urls: [String]?
    public var objective: String?

    enum CodingKeys: String, CodingKey {
        case urls, objective
        case searchQueries = "search_queries"
    }
}

public struct Search: Sendable, Hashable, Codable {
    public var tool: String
    public var arguments: SearchArguments
    public var status: String
    public var error: String?
}

/// running, complete, failed, stopped, or interrupted.
public typealias AttemptStatus = String

public struct Attempt: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var chatId: String
    public var messageId: String
    public var requestId: String
    public var requestedModel: String
    public var actualModel: String?
    public var thinking: Thinking?
    public var status: AttemptStatus
    public var content: String
    public var error: String?
    public var activity: [ActivityRecord]?
    public var mode: ChatMode?
    public var reasoning: String?
    public var reasoningTruncated: Bool?
    public var revision: Int?
    public var sources: [Source]?
    public var searches: [Search]?
    public var unsaved: Bool?
    public var phase: String?

    public init(
        id: String, chatId: String, messageId: String, requestId: String,
        requestedModel: String, actualModel: String? = nil, thinking: Thinking? = nil,
        status: AttemptStatus, content: String = "", error: String? = nil,
        activity: [ActivityRecord]? = nil, mode: ChatMode? = nil, reasoning: String? = nil,
        reasoningTruncated: Bool? = nil, revision: Int? = nil, sources: [Source]? = nil,
        searches: [Search]? = nil, unsaved: Bool? = nil, phase: String? = nil
    ) {
        self.id = id
        self.chatId = chatId
        self.messageId = messageId
        self.requestId = requestId
        self.requestedModel = requestedModel
        self.actualModel = actualModel
        self.thinking = thinking
        self.status = status
        self.content = content
        self.error = error
        self.activity = activity
        self.mode = mode
        self.reasoning = reasoning
        self.reasoningTruncated = reasoningTruncated
        self.revision = revision
        self.sources = sources
        self.searches = searches
        self.unsaved = unsaved
        self.phase = phase
    }

    enum CodingKeys: String, CodingKey {
        case id, thinking, status, content, error, activity, mode, reasoning, revision
        case sources, searches, unsaved, phase
        case chatId = "chat_id"
        case messageId = "message_id"
        case requestId = "request_id"
        case requestedModel = "requested_model"
        case actualModel = "actual_model"
        case reasoningTruncated = "reasoning_truncated"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        chatId = try c.decode(String.self, forKey: .chatId)
        messageId = try c.decode(String.self, forKey: .messageId)
        requestId = try c.decode(String.self, forKey: .requestId)
        requestedModel = (try? c.decode(String.self, forKey: .requestedModel)) ?? ""
        actualModel = try c.decodeIfPresent(String.self, forKey: .actualModel)
        thinking = try? c.decodeIfPresent(Thinking.self, forKey: .thinking)
        status = try c.decode(String.self, forKey: .status)
        content = (try c.decodeIfPresent(String.self, forKey: .content)) ?? ""
        error = try c.decodeIfPresent(String.self, forKey: .error)
        activity = try c.decodeIfPresent([ActivityRecord].self, forKey: .activity)
        mode = try? c.decodeIfPresent(ChatMode.self, forKey: .mode)
        reasoning = try c.decodeIfPresent(String.self, forKey: .reasoning)
        reasoningTruncated = try c.decodeIfPresent(Bool.self, forKey: .reasoningTruncated)
        revision = try c.decodeIfPresent(Int.self, forKey: .revision)
        sources = try c.decodeIfPresent([Source].self, forKey: .sources)
        searches = try c.decodeIfPresent([Search].self, forKey: .searches)
        unsaved = try c.decodeIfPresent(Bool.self, forKey: .unsaved)
        phase = try c.decodeIfPresent(String.self, forKey: .phase)
    }
}

public struct Message: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var text: String
    public var created: Double
    public var attempts: [Attempt]

    public init(id: String, text: String, created: Double = 0, attempts: [Attempt]) {
        self.id = id
        self.text = text
        self.created = created
        self.attempts = attempts
    }
}

public struct Attachment: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var chatId: String
    public var messageId: String?
    public var name: String
    public var mediaType: String
    public var width: Int?
    public var height: Int?
    public var size: Int
    public var kind: String?
    public var extractedText: String?
    public var extractionNotice: String?

    public var isDocument: Bool { kind == "document" }

    enum CodingKeys: String, CodingKey {
        case id, name, width, height, size, kind
        case chatId = "chat_id"
        case messageId = "message_id"
        case mediaType = "media_type"
        case extractedText = "extracted_text"
        case extractionNotice = "extraction_notice"
    }
}

public struct Folder: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var name: String
}

public struct ChatSummary: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var title: String
    public var mode: ChatMode?
    public var folderId: String?
    public var model: String?
    public var thinking: [String: Thinking?]
    public var draft: String
    public var draftRevision: Int
    public var created: Double
    public var updated: Double

    enum CodingKeys: String, CodingKey {
        case id, title, mode, model, thinking, draft, created, updated
        case folderId = "folder_id"
        case draftRevision = "draft_revision"
    }

    public init(
        id: String, title: String, mode: ChatMode? = nil, folderId: String? = nil,
        model: String? = nil, thinking: [String: Thinking?] = [:], draft: String = "",
        draftRevision: Int = 0, created: Double = 0, updated: Double = 0
    ) {
        self.id = id
        self.title = title
        self.mode = mode
        self.folderId = folderId
        self.model = model
        self.thinking = thinking
        self.draft = draft
        self.draftRevision = draftRevision
        self.created = created
        self.updated = updated
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        title = try c.decode(String.self, forKey: .title)
        mode = try? c.decodeIfPresent(ChatMode.self, forKey: .mode)
        folderId = try c.decodeIfPresent(String.self, forKey: .folderId)
        model = try c.decodeIfPresent(String.self, forKey: .model)
        thinking = (try? c.decodeIfPresent([String: Thinking?].self, forKey: .thinking)) ?? [:]
        draft = (try c.decodeIfPresent(String.self, forKey: .draft)) ?? ""
        draftRevision = (try c.decodeIfPresent(Int.self, forKey: .draftRevision)) ?? 0
        created = (try c.decodeIfPresent(Double.self, forKey: .created)) ?? 0
        updated = (try c.decodeIfPresent(Double.self, forKey: .updated)) ?? 0
    }
}

public struct Chat: Sendable, Hashable, Codable, Identifiable {
    public var summary: ChatSummary
    public var messages: [Message]
    public var attachments: [Attachment]

    public var id: String { summary.id }
    public var title: String { summary.title }
    public var mode: ChatMode { summary.mode ?? .normal }
    public var model: String? { summary.model }

    public init(summary: ChatSummary, messages: [Message], attachments: [Attachment] = []) {
        self.summary = summary
        self.messages = messages
        self.attachments = attachments
    }

    enum CodingKeys: String, CodingKey { case messages, attachments }

    public init(from decoder: Decoder) throws {
        summary = try ChatSummary(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        messages = try c.decode([Message].self, forKey: .messages)
        attachments = (try c.decodeIfPresent([Attachment].self, forKey: .attachments)) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        try summary.encode(to: encoder)
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(messages, forKey: .messages)
        try c.encode(attachments, forKey: .attachments)
    }
}

public struct LocalModel: Sendable, Hashable, Codable, Identifiable {
    public var name: String
    public var available: Bool
    public var reason: String?
    public var vision: Bool
    public var canThink: Bool
    public var tools: Bool?
    public var thinking: [Thinking]
    public var thinkingDefault: Thinking?
    public var size: Double?
    public var contextLength: Int?
    public var provider: String?
    public var advertisedName: String?

    public var id: String { name }
    public var isCloud: Bool { provider == "openrouter" || name.hasPrefix("openrouter:") }
    public var providerLabel: String { isCloud ? "OpenRouter" : "Local Ollama" }
    public var displayName: String {
        if let advertisedName { return advertisedName }
        if isCloud { return name.hasPrefix("openrouter:") ? String(name.dropFirst("openrouter:".count)) : name }
        return formatModelName(name.replacingOccurrences(of: "^ollama:", with: "", options: .regularExpression))
    }

    public func matches(_ selection: String) -> Bool {
        if isCloud { return name == selection }
        return name.replacingOccurrences(of: "^ollama:", with: "", options: .regularExpression)
            == selection.replacingOccurrences(of: "^ollama:", with: "", options: .regularExpression)
    }

    public init(
        name: String, available: Bool = true, reason: String? = nil, vision: Bool = false,
        canThink: Bool = false, tools: Bool? = nil, thinking: [Thinking] = [],
        thinkingDefault: Thinking? = nil, size: Double? = nil, contextLength: Int? = nil
    ) {
        self.name = name
        self.available = available
        self.reason = reason
        self.vision = vision
        self.canThink = canThink
        self.tools = tools
        self.thinking = thinking
        self.thinkingDefault = thinkingDefault
        self.size = size
        self.contextLength = contextLength
        self.provider = nil
        self.advertisedName = nil
    }

    enum CodingKeys: String, CodingKey {
        case name, available, reason, vision, tools, thinking, size, provider
        case canThink = "can_think"
        case thinkingDefault = "thinking_default"
        case contextLength = "context_length"
        case advertisedName = "display_name"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        available = try c.decode(Bool.self, forKey: .available)
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        vision = (try c.decodeIfPresent(Bool.self, forKey: .vision)) ?? false
        canThink = (try c.decodeIfPresent(Bool.self, forKey: .canThink)) ?? false
        tools = try c.decodeIfPresent(Bool.self, forKey: .tools)
        thinking = (try? c.decodeIfPresent([Thinking].self, forKey: .thinking)) ?? []
        thinkingDefault = try? c.decodeIfPresent(Thinking.self, forKey: .thinkingDefault)
        size = try c.decodeIfPresent(Double.self, forKey: .size)
        contextLength = try c.decodeIfPresent(Int.self, forKey: .contextLength)
        provider = try c.decodeIfPresent(String.self, forKey: .provider)
        advertisedName = try c.decodeIfPresent(String.self, forKey: .advertisedName)
    }
}

/// The reply that is generating now.
public struct Active: Sendable, Hashable, Codable {
    public var chatId: String
    public var attemptId: String
    public var requestId: String

    public init(chatId: String, attemptId: String, requestId: String) {
        self.chatId = chatId
        self.attemptId = attemptId
        self.requestId = requestId
    }

    enum CodingKeys: String, CodingKey {
        case chatId = "chat_id"
        case attemptId = "attempt_id"
        case requestId = "request_id"
    }
}

/// Who drives the browser: Vikram, or the person who took it over.
public enum BrowserControl: String, Sendable, Codable {
    case agent, person
}

public struct Snapshot: Sendable, Decodable {
    public var `protocol`: Int?
    public var chats: [ChatSummary]
    public var folders: [Folder]
    public var preferences: [String: JSONValue]
    public var active: Active?
    public var models: [LocalModel]
    public var browser: BrowserState?

    public struct BrowserState: Sendable, Decodable {
        public var control: BrowserControl
    }
}

/// A runtime event: a reply's progress, a mode change, a notice, or a lost service.
public struct RuntimeEvent: Sendable, Decodable {
    public var event: String
    public var chatId: String?
    public var attemptId: String?
    public var requestId: String?
    public var control: BrowserControl?
    public var activity: [ActivityRecord]?
    public var mode: ChatMode?
    public var reasoning: String?
    public var reasoningTruncated: Bool?
    public var revision: Int?
    public var sources: [Source]?
    public var searches: [Search]?
    public var status: AttemptStatus?
    public var content: String?
    public var actualModel: String?
    public var phase: String?
    public var error: String?
    public var unsaved: Bool?
    public var message: String?

    public init(
        event: String, chatId: String? = nil, attemptId: String? = nil,
        requestId: String? = nil, status: AttemptStatus? = nil, content: String? = nil,
        revision: Int? = nil, unsaved: Bool? = nil, error: String? = nil,
        activity: [ActivityRecord]? = nil, sources: [Source]? = nil,
        reasoning: String? = nil, phase: String? = nil
    ) {
        self.event = event
        self.chatId = chatId
        self.attemptId = attemptId
        self.requestId = requestId
        self.status = status
        self.content = content
        self.revision = revision
        self.unsaved = unsaved
        self.error = error
        self.activity = activity
        self.sources = sources
        self.reasoning = reasoning
        self.phase = phase
    }

    enum CodingKeys: String, CodingKey {
        case event, control, activity, mode, reasoning, revision, sources, searches
        case status, content, phase, error, unsaved, message
        case chatId = "chat_id"
        case attemptId = "attempt_id"
        case requestId = "request_id"
        case reasoningTruncated = "reasoning_truncated"
        case actualModel = "actual_model"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        event = try c.decode(String.self, forKey: .event)
        chatId = try? c.decodeIfPresent(String.self, forKey: .chatId)
        attemptId = try? c.decodeIfPresent(String.self, forKey: .attemptId)
        requestId = try? c.decodeIfPresent(String.self, forKey: .requestId)
        control = try? c.decodeIfPresent(BrowserControl.self, forKey: .control)
        activity = try? c.decodeIfPresent([ActivityRecord].self, forKey: .activity)
        mode = try? c.decodeIfPresent(ChatMode.self, forKey: .mode)
        reasoning = try? c.decodeIfPresent(String.self, forKey: .reasoning)
        reasoningTruncated = try? c.decodeIfPresent(Bool.self, forKey: .reasoningTruncated)
        revision = try? c.decodeIfPresent(Int.self, forKey: .revision)
        sources = try? c.decodeIfPresent([Source].self, forKey: .sources)
        searches = try? c.decodeIfPresent([Search].self, forKey: .searches)
        status = try? c.decodeIfPresent(String.self, forKey: .status)
        content = try? c.decodeIfPresent(String.self, forKey: .content)
        actualModel = try? c.decodeIfPresent(String.self, forKey: .actualModel)
        phase = try? c.decodeIfPresent(String.self, forKey: .phase)
        error = try? c.decodeIfPresent(String.self, forKey: .error)
        unsaved = try? c.decodeIfPresent(Bool.self, forKey: .unsaved)
        message = try? c.decodeIfPresent(String.self, forKey: .message)
    }
}

// MARK: Scheduled tasks

public struct ScheduleTime: Sendable, Hashable, Codable {
    /// once, daily, weekly, monthly, or interval.
    public var repeatKind: String
    public var time: String?
    public var date: String?
    public var days: [String]?
    public var dayOfMonth: Int?
    public var everyMinutes: Int?

    public init(
        repeatKind: String, time: String? = nil, date: String? = nil, days: [String]? = nil,
        dayOfMonth: Int? = nil, everyMinutes: Int? = nil
    ) {
        self.repeatKind = repeatKind
        self.time = time
        self.date = date
        self.days = days
        self.dayOfMonth = dayOfMonth
        self.everyMinutes = everyMinutes
    }

    enum CodingKeys: String, CodingKey {
        case time, date, days
        case repeatKind = "repeat"
        case dayOfMonth = "day_of_month"
        case everyMinutes = "every_minutes"
    }

    /// Only the fields this kind of schedule uses, as the service expects.
    public var fields: JSONValue {
        var object: [String: JSONValue] = ["repeat": .string(repeatKind)]
        func put(_ key: String, _ value: JSONValue?) {
            if let value { object[key] = value }
        }
        switch repeatKind {
        case "once":
            put("date", date.map(JSONValue.string))
            put("time", time.map(JSONValue.string))
        case "daily":
            put("time", time.map(JSONValue.string))
        case "weekly":
            put("days", days.map { .array($0.map(JSONValue.string)) })
            put("time", time.map(JSONValue.string))
        case "monthly":
            put("day_of_month", dayOfMonth.map { .number(Double($0)) })
            put("time", time.map(JSONValue.string))
        default:
            put("every_minutes", everyMinutes.map { .number(Double($0)) })
        }
        return .object(object)
    }
}

public struct RunSummary: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    /// schedule, catch_up, or manual.
    public var trigger: String
    public var scheduledFor: Double?
    public var started: Double
    public var finished: Double?
    /// running, complete, failed, stopped, interrupted, or missed.
    public var status: String
    public var error: String?
    public var model: String?
    public var actualModel: String?
    public var preview: String?
    /// A bot run's derived outcome: quiet, updated, needs-attention, blocked,
    /// failed, or incomplete; nil for a scheduled prompt's run.
    public var outcome: String?
    /// A bot run's one-line report.
    public var report: String?

    enum CodingKeys: String, CodingKey {
        case id, trigger, started, finished, status, error, model, preview, outcome, report
        case scheduledFor = "scheduled_for"
        case actualModel = "actual_model"
    }
}

public struct LastRun: Sendable, Hashable, Codable {
    public var id: String
    public var status: String
    public var started: Double
    public var outcome: String?
}

public struct ScheduledTask: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var name: String
    public var prompt: String
    public var schedule: ScheduleTime
    public var description: String
    public var model: String?
    public var thinking: Thinking?
    public var enabled: Bool
    /// app, chat, or cli.
    public var origin: String
    public var nextRun: Double?
    public var runRequested: Bool
    /// scheduled, paused, or done.
    public var state: String
    public var created: Double
    public var updated: Double
    public var lastRun: LastRun?
    /// Present when the service sends one task in detail.
    public var runs: [RunSummary]?

    enum CodingKeys: String, CodingKey {
        case id, name, prompt, schedule, description, model, thinking, enabled, origin
        case state, created, updated, runs
        case nextRun = "next_run"
        case runRequested = "run_requested"
        case lastRun = "last_run"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try c.decode(String.self, forKey: .name)
        prompt = try c.decode(String.self, forKey: .prompt)
        schedule = try c.decode(ScheduleTime.self, forKey: .schedule)
        description = (try c.decodeIfPresent(String.self, forKey: .description)) ?? ""
        model = try c.decodeIfPresent(String.self, forKey: .model)
        thinking = try? c.decodeIfPresent(Thinking.self, forKey: .thinking)
        enabled = try c.decode(Bool.self, forKey: .enabled)
        origin = (try c.decodeIfPresent(String.self, forKey: .origin)) ?? "app"
        nextRun = try c.decodeIfPresent(Double.self, forKey: .nextRun)
        runRequested = (try c.decodeIfPresent(Bool.self, forKey: .runRequested)) ?? false
        state = (try c.decodeIfPresent(String.self, forKey: .state)) ?? "scheduled"
        created = (try c.decodeIfPresent(Double.self, forKey: .created)) ?? 0
        updated = (try c.decodeIfPresent(Double.self, forKey: .updated)) ?? 0
        lastRun = try? c.decodeIfPresent(LastRun.self, forKey: .lastRun)
        runs = try c.decodeIfPresent([RunSummary].self, forKey: .runs)
    }
}

public struct TaskRun: Sendable, Hashable, Codable, Identifiable {
    public var summary: RunSummary
    public var taskId: String
    public var prompt: String
    public var content: String
    public var reasoning: String
    public var activity: [ActivityRecord]
    public var sources: [Source]
    public var searches: [Search]

    public var id: String { summary.id }

    enum CodingKeys: String, CodingKey {
        case prompt, content, reasoning, activity, sources, searches
        case taskId = "task_id"
    }

    public init(from decoder: Decoder) throws {
        summary = try RunSummary(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        taskId = try c.decode(String.self, forKey: .taskId)
        prompt = try c.decode(String.self, forKey: .prompt)
        content = (try c.decodeIfPresent(String.self, forKey: .content)) ?? ""
        reasoning = (try c.decodeIfPresent(String.self, forKey: .reasoning)) ?? ""
        activity = (try c.decodeIfPresent([ActivityRecord].self, forKey: .activity)) ?? []
        sources = (try c.decodeIfPresent([Source].self, forKey: .sources)) ?? []
        searches = (try c.decodeIfPresent([Search].self, forKey: .searches)) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        try summary.encode(to: encoder)
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(taskId, forKey: .taskId)
        try c.encode(prompt, forKey: .prompt)
        try c.encode(content, forKey: .content)
        try c.encode(reasoning, forKey: .reasoning)
        try c.encode(activity, forKey: .activity)
        try c.encode(sources, forKey: .sources)
        try c.encode(searches, forKey: .searches)
    }

    /// The run as one turn of a chat: Vikram's answer to the task's prompt.
    public var attempt: Attempt {
        Attempt(
            id: summary.id, chatId: taskId, messageId: summary.id, requestId: summary.id,
            requestedModel: summary.model ?? "", actualModel: summary.actualModel,
            status: summary.status == "missed" ? "interrupted" : summary.status,
            content: content, error: summary.error, activity: activity,
            reasoning: reasoning, sources: sources, searches: searches, phase: "answering")
    }
}

public struct ScheduleList: Sendable, Decodable {
    public var tasks: [ScheduledTask]
    /// on, off, unavailable, or failed: whether this Mac runs tasks in the background.
    public var background: String
}

// MARK: Bots

/// One thing a bot reported. Findings with the same key merge; dismissed ones
/// stay quiet until the fact behind them changes.
public struct Finding: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var taskId: String
    public var runId: String?
    public var key: String
    /// decision, approval, material-change, failure, or fyi.
    public var kind: String
    /// attention, material, or info.
    public var severity: String
    public var title: String
    public var detail: String
    public var sources: [String]
    /// new, seen, snoozed, resolved, or dismissed.
    public var state: String
    /// Held on the bot's page as uncertain; never on Home.
    public var held: Bool
    public var count: Int
    public var firstSeen: Double
    public var lastSeen: Double
    public var updated: Double
    public var snoozedUntil: Double?
    public var botName: String

    enum CodingKeys: String, CodingKey {
        case id, key, kind, severity, title, detail, sources, state, held, count, updated
        case taskId = "task_id"
        case runId = "run_id"
        case firstSeen = "first_seen"
        case lastSeen = "last_seen"
        case snoozedUntil = "snoozed_until"
        case botName = "bot_name"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        taskId = try c.decode(String.self, forKey: .taskId)
        runId = try c.decodeIfPresent(String.self, forKey: .runId)
        key = try c.decode(String.self, forKey: .key)
        kind = try c.decode(String.self, forKey: .kind)
        severity = try c.decode(String.self, forKey: .severity)
        title = try c.decode(String.self, forKey: .title)
        detail = (try c.decodeIfPresent(String.self, forKey: .detail)) ?? ""
        sources = (try c.decodeIfPresent([String].self, forKey: .sources)) ?? []
        state = try c.decode(String.self, forKey: .state)
        held = (try c.decodeIfPresent(Bool.self, forKey: .held)) ?? false
        count = (try c.decodeIfPresent(Int.self, forKey: .count)) ?? 1
        firstSeen = (try c.decodeIfPresent(Double.self, forKey: .firstSeen)) ?? 0
        lastSeen = (try c.decodeIfPresent(Double.self, forKey: .lastSeen)) ?? 0
        updated = (try c.decodeIfPresent(Double.self, forKey: .updated)) ?? 0
        snoozedUntil = try c.decodeIfPresent(Double.self, forKey: .snoozedUntil)
        botName = (try c.decodeIfPresent(String.self, forKey: .botName)) ?? ""
    }
}

/// A bot as the service lists it; `config`, `findings`, `runs`, and
/// `lastSummary` are present when the service sends one bot in detail.
public struct Bot: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var name: String
    public var mandate: String
    public var template: String?
    public var description: String
    public var schedule: ScheduleTime
    public var model: String?
    /// Explicit OpenRouter opt-in for this bot.
    public var cloud: Bool
    public var enabled: Bool
    /// scheduled, paused, or done.
    public var state: String
    public var nextRun: Double?
    public var runRequested: Bool
    public var lastRun: LastRun?
    public var lastComplete: Double?
    public var expectedDays: Int?
    /// No completed run inside the expected period.
    public var overdue: Bool
    /// attention, findings, or never.
    public var notify: String
    public var budgetMinutes: Int
    public var attention: Int
    public var material: Int
    public var info: Int
    public var held: Int
    public var created: Double
    public var updated: Double
    public var config: JSONValue?
    public var findings: [Finding]?
    public var runs: [RunSummary]?
    public var lastSummary: String?

    enum CodingKeys: String, CodingKey {
        case id, name, mandate, template, description, schedule, model, cloud, enabled, state
        case overdue, notify, attention, material, info, held, created, updated, config, findings
        case runs
        case nextRun = "next_run"
        case runRequested = "run_requested"
        case lastRun = "last_run"
        case lastComplete = "last_complete"
        case expectedDays = "expected_days"
        case budgetMinutes = "budget_minutes"
        case lastSummary = "last_summary"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try c.decode(String.self, forKey: .name)
        mandate = (try c.decodeIfPresent(String.self, forKey: .mandate)) ?? ""
        template = try c.decodeIfPresent(String.self, forKey: .template)
        description = (try c.decodeIfPresent(String.self, forKey: .description)) ?? ""
        schedule = try c.decode(ScheduleTime.self, forKey: .schedule)
        model = try c.decodeIfPresent(String.self, forKey: .model)
        cloud = (try c.decodeIfPresent(Bool.self, forKey: .cloud)) ?? false
        enabled = (try c.decodeIfPresent(Bool.self, forKey: .enabled)) ?? true
        state = (try c.decodeIfPresent(String.self, forKey: .state)) ?? "scheduled"
        nextRun = try c.decodeIfPresent(Double.self, forKey: .nextRun)
        runRequested = (try c.decodeIfPresent(Bool.self, forKey: .runRequested)) ?? false
        lastRun = try? c.decodeIfPresent(LastRun.self, forKey: .lastRun)
        lastComplete = try c.decodeIfPresent(Double.self, forKey: .lastComplete)
        expectedDays = try c.decodeIfPresent(Int.self, forKey: .expectedDays)
        overdue = (try c.decodeIfPresent(Bool.self, forKey: .overdue)) ?? false
        notify = (try c.decodeIfPresent(String.self, forKey: .notify)) ?? "attention"
        budgetMinutes = (try c.decodeIfPresent(Int.self, forKey: .budgetMinutes)) ?? 10
        attention = (try c.decodeIfPresent(Int.self, forKey: .attention)) ?? 0
        material = (try c.decodeIfPresent(Int.self, forKey: .material)) ?? 0
        info = (try c.decodeIfPresent(Int.self, forKey: .info)) ?? 0
        held = (try c.decodeIfPresent(Int.self, forKey: .held)) ?? 0
        created = (try c.decodeIfPresent(Double.self, forKey: .created)) ?? 0
        updated = (try c.decodeIfPresent(Double.self, forKey: .updated)) ?? 0
        config = try c.decodeIfPresent(JSONValue.self, forKey: .config)
        findings = try c.decodeIfPresent([Finding].self, forKey: .findings)
        runs = try c.decodeIfPresent([RunSummary].self, forKey: .runs)
        lastSummary = try c.decodeIfPresent(String.self, forKey: .lastSummary)
    }
}

/// The Home page: what needs you, what changed, and every bot's health.
public struct Home: Sendable, Decodable {
    public var needsYou: [Finding]
    public var material: [Finding]
    /// Bots with no completed run inside their expected period.
    public var stale: [Bot]
    public var bots: [Bot]

    enum CodingKeys: String, CodingKey {
        case material, stale, bots
        case needsYou = "needs_you"
    }

    public static let empty = Home(needsYou: [], material: [], stale: [], bots: [])

    public init(needsYou: [Finding], material: [Finding], stale: [Bot], bots: [Bot]) {
        self.needsYou = needsYou
        self.material = material
        self.stale = stale
        self.bots = bots
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        needsYou = (try c.decodeIfPresent([Finding].self, forKey: .needsYou)) ?? []
        material = (try c.decodeIfPresent([Finding].self, forKey: .material)) ?? []
        stale = (try c.decodeIfPresent([Bot].self, forKey: .stale)) ?? []
        bots = (try c.decodeIfPresent([Bot].self, forKey: .bots)) ?? []
    }

    /// Nothing waits for the person.
    public var isCalm: Bool { needsYou.isEmpty && stale.isEmpty }
}

public struct BotTemplate: Sendable, Hashable, Decodable, Identifiable {
    public var id: String
    public var title: String
    public var summary: String
    public var available: Bool
    /// Why an unavailable template cannot be used yet.
    public var reason: String?
    /// Server-owned defaults; absent for blocked templates or an older sidecar.
    public var defaults: JSONValue?
}

public struct BotList: Sendable, Decodable {
    public var bots: [Bot]
    public var background: String
}

public struct TemplateList: Sendable, Decodable {
    public var templates: [BotTemplate]
}

// MARK: Updates

/// The installed app's updates, as its service reports them.
public struct UpdateStatus: Sendable, Hashable, Decodable {
    /// unavailable, idle, checking, installing, current, ready, or failed.
    public var state: String
    public var running: String?
    public var version: String?
    public var error: String?
}
