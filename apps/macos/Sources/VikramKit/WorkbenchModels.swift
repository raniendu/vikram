import Foundation

public enum QueueKind: String, Sendable, Codable, CaseIterable {
    case steer, followup

    public var label: String { self == .steer ? "Steering" : "Follow-up" }
}

public struct QueuedInput: Sendable, Hashable, Codable, Identifiable {
    public var id: String
    public var text: String
    public var kind: QueueKind
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
    public var trusted: Bool
    public var provider: String
    public var cloudEnabled: Bool
    public var credentials: String?
    public var context: [String: JSONValue]
    public var queue: [QueuedInput]
    public var resources: [String: JSONValue]
    public var mcp: [IntegrationStatus]
    public var extensions: [IntegrationStatus]

    enum CodingKeys: String, CodingKey {
        case workspace, trusted, provider, credentials, context, queue, resources, mcp, extensions
        case cloudEnabled = "cloud_enabled"
        case defaultModel = "default_model"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        defaultModel = try c.decodeIfPresent(String.self, forKey: .defaultModel)
        workspace = try c.decodeIfPresent(String.self, forKey: .workspace)
        trusted = try c.decodeIfPresent(Bool.self, forKey: .trusted) ?? false
        provider = try c.decodeIfPresent(String.self, forKey: .provider) ?? "ollama"
        cloudEnabled = try c.decodeIfPresent(Bool.self, forKey: .cloudEnabled) ?? false
        credentials = try c.decodeIfPresent(String.self, forKey: .credentials)
        context = try c.decodeIfPresent([String: JSONValue].self, forKey: .context) ?? [:]
        queue = try c.decodeIfPresent([QueuedInput].self, forKey: .queue) ?? []
        resources = try c.decodeIfPresent([String: JSONValue].self, forKey: .resources) ?? [:]
        mcp = try c.decodeIfPresent([IntegrationStatus].self, forKey: .mcp) ?? []
        extensions = try c.decodeIfPresent([IntegrationStatus].self, forKey: .extensions) ?? []
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
