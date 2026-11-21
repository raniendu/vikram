import Foundation

public enum QueueKind: String, Sendable, Codable, CaseIterable {
    case steer, followup

    public var label: String { self == .steer ? "Steering" : "Follow-up" }
}

public struct QueuedInput: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var text: String
    public var kind: QueueKind
    /// Paused after its reply stopped or failed, or when it could not start: it
    /// waits for the person instead of the next free reply slot.
    public var held: Bool?

    public var isPaused: Bool { held == true }
}

public struct ConversationBranch: Sendable, Hashable, Decodable, Identifiable {
    public var id: String
    public var text: String
    public var parentId: String?
    public var active: Bool?

    enum CodingKeys: String, CodingKey {
        case id, text, active
        case parentId = "parent_id"
    }
}

/// How much of the chat model's context window the conversation fills.
public struct ContextUsage: Sendable, Hashable {
    public enum Level: Sendable, Hashable { case normal, warning, critical }

    /// The model's own count for the latest request, or a text estimate.
    public var tokens: Int
    public var limit: Int
    public var estimated: Bool

    public init(tokens: Int, limit: Int, estimated: Bool) {
        self.tokens = tokens
        self.limit = limit
        self.estimated = estimated
    }

    public var fraction: Double { limit > 0 ? min(max(Double(tokens) / Double(limit), 0), 1) : 0 }

    /// Automatic compaction starts at 80%; past 95% a request may not fit.
    public var level: Level { fraction >= 0.95 ? .critical : fraction >= 0.8 ? .warning : .normal }

    public var percentText: String {
        fraction > 0 && fraction < 0.01
            ? "<1%" : fraction.formatted(.percent.precision(.fractionLength(0)).rounded(rule: .down))
    }
}

public struct IntegrationStatus: Sendable, Hashable, Decodable, Identifiable {
    public var name: String
    public var status: String?
    public var enabled: Bool?
    public var toolCount: Int?
    public var source: String?
    public var version: String?
    public var error: String?
    public var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, status, enabled, source, version, error
        case toolCount = "tool_count"
    }
}

public struct WorkbenchStatus: Sendable, Decodable {
    public var defaultModel: String?
    public var workspace: String?
    public var sandbox: [String: JSONValue]
    public var trusted: Bool
    public var provider: String
    public var cloudEnabled: Bool
    public var credentials: String?
    public var context: [String: JSONValue]
    public var queue: [QueuedInput]
    /// Messages queued in every chat; the service keeps them only while it runs.
    public var queueTotal: Int
    public var resources: [String: JSONValue]
    public var mcp: [IntegrationStatus]
    public var extensions: [IntegrationStatus]

    enum CodingKeys: String, CodingKey {
        case workspace, sandbox, trusted, provider, credentials, context, queue, resources, mcp, extensions
        case cloudEnabled = "cloud_enabled"
        case defaultModel = "default_model"
        case queueTotal = "queue_total"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        defaultModel = try container.decodeIfPresent(String.self, forKey: .defaultModel)
        workspace = try container.decodeIfPresent(String.self, forKey: .workspace)
        sandbox = try container.decodeIfPresent([String: JSONValue].self, forKey: .sandbox) ?? [:]
        trusted = try container.decodeIfPresent(Bool.self, forKey: .trusted) ?? false
        provider = try container.decodeIfPresent(String.self, forKey: .provider) ?? "ollama"
        cloudEnabled = try container.decodeIfPresent(Bool.self, forKey: .cloudEnabled) ?? false
        credentials = try container.decodeIfPresent(String.self, forKey: .credentials)
        context = try container.decodeIfPresent([String: JSONValue].self, forKey: .context) ?? [:]
        queue = try container.decodeIfPresent([QueuedInput].self, forKey: .queue) ?? []
        queueTotal = try container.decodeIfPresent(Int.self, forKey: .queueTotal) ?? queue.count
        resources = try container.decodeIfPresent([String: JSONValue].self, forKey: .resources) ?? [:]
        mcp = try container.decodeIfPresent([IntegrationStatus].self, forKey: .mcp) ?? []
        extensions = try container.decodeIfPresent([IntegrationStatus].self, forKey: .extensions) ?? []
    }
}

/// A host, MCP, or extension tool invocation and its explicit approval request.
public struct ToolRecord: Sendable, Hashable, Codable {
    public var kind: String
    public var id: String
    public var status: String
    public var name: String?
    public var tool: String?
    public var action: String?
    public var summary: String?
    public var reason: String?
    public var url: String?
    public var diff: String?
    public var output: JSONValue?
    public var error: String?
    public var approval: BrowserApprovalInfo?
    public var ui: [JSONValue]?

    public var title: String {
        summary ?? approval?.summary ?? name ?? tool ?? action ?? kind.capitalized
    }

    public var outputText: String? {
        guard let output, output != .null else { return nil }
        if let string = output.string { return string }
        return (try? output.encoded()).map { String(decoding: $0, as: UTF8.self) }
    }
}
