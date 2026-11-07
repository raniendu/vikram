import Foundation
import Observation

/// Native controls for the same workbench operations used by the terminal.
@MainActor @Observable
public final class WorkbenchController {
    public private(set) var status: WorkbenchStatus?
    public private(set) var branches: [ConversationBranch] = []
    public private(set) var working = false
    public var error: String?
    @ObservationIgnored private let service: ServiceConnection
    @ObservationIgnored private let chats: ChatController
    @ObservationIgnored private var statusChatId: String?

    public init(service: ServiceConnection, chats: ChatController) {
        self.service = service
        self.chats = chats
    }

    private func fields(
        _ action: String, chatId: String?, value: String? = nil,
        options: [String: JSONValue] = [:]
    ) -> [String: JSONValue] {
        var result: [String: JSONValue] = ["action": .string(action)]
        if let chatId { result["chat_id"] = .string(chatId) }
        if let value { result["value"] = .string(value) }
        if !options.isEmpty { result["options"] = .object(options) }
        return result
    }

    private func fail(_ error: Error) {
        self.error = (error as? ServiceError)?.message ?? "This workbench action could not finish. Try again."
    }

    public func refresh(chatId: String?) async {
        if statusChatId != chatId {
            status = nil
            branches = []
        }
        statusChatId = chatId
        do {
            let loaded = try await service.call("workbench", fields("status", chatId: chatId))
                .decode(WorkbenchStatus.self)
            if statusChatId == chatId { status = loaded }
        } catch {
            if statusChatId == chatId { fail(error) }
        }
    }

    @discardableResult
    public func perform(
        _ action: String, value: String? = nil, options: [String: JSONValue] = [:]
    ) async -> JSONValue? {
        guard !working else { return nil }
        working = true
        error = nil
        defer { working = false }
        let chatId = chats.selected
        do {
            if ["branch", "fork", "clone", "import"].contains(action) {
                try await chats.flushDrafts()
            }
            let result = try await service.call(
                "workbench", fields(action, chatId: chatId, value: value, options: options))
            if let changed = try? result.decode(Chat.self) {
                chats.put(changed)
                _ = try await chats.snapshot()
                if ["fork", "clone", "import"].contains(action) {
                    await chats.select(changed.id)
                }
            }
            if action == "tree" { branches = try result.decode([ConversationBranch].self) }
            if ["cloud", "login", "logout"].contains(action) { await chats.refreshModels() }
            await refresh(chatId: chats.selected)
            return result
        } catch {
            fail(error)
            return nil
        }
    }

    /// Queue accepted text without clearing a draft edited while the RPC was in flight.
    public func queueDraft(kind: QueueKind) async -> Bool {
        let text = chats.draft
        let chatId = chats.selected
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return false }
        guard await perform("queue", value: text, options: ["kind": .string(kind.rawValue)]) != nil else {
            return false
        }
        if chats.selected == chatId, chats.draft == text { chats.changeDraft("") }
        return true
    }
}
