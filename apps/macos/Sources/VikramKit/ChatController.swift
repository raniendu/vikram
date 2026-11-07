import Foundation
import Observation

/// The person's answer to a waiting question, plan, browser action, or task.
public enum Reply: Sendable {
    case answers([QuestionAnswer])
    case skip
    case approve
    case dismiss
    case revise(feedback: String)

    var fields: [String: JSONValue] {
        switch self {
        case .answers(let answers): ["answers": .array(answers.map(\.json))]
        case .skip: ["cancelled": true]
        case .approve: ["decision": "approve"]
        case .dismiss: ["decision": "dismiss"]
        case .revise(let feedback): ["decision": "revise", "feedback": .string(feedback)]
        }
    }
}

public struct QuestionAnswer: Sendable, Hashable {
    public var header: String
    public var selected: [String]
    public var customAnswer: String?

    public init(header: String, selected: [String] = [], customAnswer: String? = nil) {
        self.header = header
        self.selected = selected
        self.customAnswer = customAnswer
    }

    var json: JSONValue {
        if let customAnswer {
            return ["header": .string(header), "custom_answer": .string(customAnswer)]
        }
        return ["header": .string(header), "selected": .array(selected.map(JSONValue.string))]
    }
}

/// Saved chats, the open chat, drafts, and the one reply that may be running.
@MainActor @Observable
public final class ChatController {
    public private(set) var chats: [ChatSummary] = []
    public private(set) var folders: [Folder] = []
    public private(set) var models: [LocalModel] = []
    public private(set) var cache: [String: Chat] = [:]
    public private(set) var selected: String?
    public private(set) var active: Active?
    public private(set) var preferences: [String: JSONValue] = [:]
    public var error: String?
    public private(set) var connection: String?
    public private(set) var offline = true
    public private(set) var loading = true
    public private(set) var sending = false
    public private(set) var importing = false
    public private(set) var refreshing = false
    public private(set) var browserControl: BrowserControl = .agent
    public private(set) var phase: String?
    public private(set) var drafts: [String: String] = [:]

    @ObservationIgnored private let service: ServiceConnection
    @ObservationIgnored private var draftRecords: [String: DraftRecord] = [:]
    @ObservationIgnored private var timers: [String: Task<Void, Never>] = [:]
    @ObservationIgnored private var events: [String: RuntimeEvent] = [:]
    @ObservationIgnored private var submissions: [String: Submission] = [:]
    @ObservationIgnored private var eventVersion = 0
    @ObservationIgnored private var sendLock = false

    public nonisolated static let maxFileSize = 10 * 1024 * 1024
    public nonisolated static let maxFiles = 4

    public init(service: ServiceConnection) {
        self.service = service
    }

    // MARK: Reading

    public var chat: Chat? { selected.flatMap { cache[$0] } }
    public var draft: String { selected.flatMap { drafts[$0] } ?? "" }

    /// The open chat's explicitly selected model, when its provider has it.
    public var model: LocalModel? {
        guard let name = chat?.model else { return nil }
        return models.first { $0.matches(name) && $0.available }
    }

    public var lastModel: String? { preferences["last_model"]?.string }

    func rpc(_ method: String, _ params: [String: JSONValue] = [:]) async throws -> JSONValue {
        try await service.call(method, params)
    }

    func rpc<T: Decodable>(_ type: T.Type, _ method: String, _ params: [String: JSONValue] = [:])
        async throws -> T
    {
        try await service.call(method, params).decode(T.self)
    }

    func fail(_ error: Error) {
        self.error =
            (error as? ServiceError)?.message ?? "This action could not finish. Please try again."
    }

    // MARK: State

    func put(_ chat: Chat, recovery: Bool = false) {
        var merged = recovery ? recoverChat(chat, events) : chat
        if recovery {
            for message in merged.messages {
                for attempt in message.attempts where attempt.unsaved != true {
                    events.removeValue(forKey: attempt.id)
                }
            }
        } else {
            for event in events.values { merged = applyAttempt(merged, event) }
        }
        cache[chat.id] = merged
        chats = ([chat.summary] + chats.filter { $0.id != chat.id })
            .sorted { $0.updated > $1.updated }
        if draftRecords[chat.id] == nil {
            draftRecords[chat.id] = DraftRecord(
                text: chat.summary.draft, revision: chat.summary.draftRevision)
            drafts[chat.id] = chat.summary.draft
        }
    }

    @discardableResult
    func loadChat(_ id: String, recovery: Bool = false) async throws -> Chat {
        let chat = try await rpc(Chat.self, "chat", ["chat_id": .string(id)])
        put(chat, recovery: recovery)
        return chat
    }

    @discardableResult
    func snapshot() async throws -> Snapshot {
        let version = eventVersion
        let state = try await rpc(Snapshot.self, "snapshot")
        chats = state.chats
        folders = state.folders
        if eventVersion == version { active = reconcileActive(state.active, events) }
        return state
    }

    public func refreshModels() async {
        refreshing = true
        defer { refreshing = false }
        do {
            let state = try await rpc("refresh")
            models = try state["models"]?.decode([LocalModel].self) ?? []
            connection = state["connection_error"]?.string
        } catch {
            fail(error)
        }
    }

    /// Open the service (or restart it) and show the library.
    public func boot(reconnect: Bool = false) async {
        loading = true
        defer { loading = false }
        do {
            let value = reconnect ? try await service.reconnect() : try await rpc("hello")
            let state = try value.decode(Snapshot.self)
            guard state.protocol == 1 else {
                throw ServiceError(
                    "The desktop service version does not match this app. Rebuild or update Vikram."
                )
            }
            chats = state.chats
            folders = state.folders
            active = reconcileActive(state.active, events)
            preferences = state.preferences
            browserControl = state.browser?.control ?? .agent
            offline = false
            let previous = selected ?? state.preferences["selected_chat"]?.string
            let id = state.chats.contains { $0.id == previous } ? previous : state.chats.first?.id
            if let id {
                selected = id
                try await loadChat(id, recovery: reconnect)
            }
            await refreshModels()
            // Local drafts survive a service restart; rebase their revisions on saved records.
            if reconnect {
                for chat in state.chats where draftRecords[chat.id] != nil {
                    let saved = try await rpc(Chat.self, "chat", ["chat_id": .string(chat.id)])
                    guard let draft = draftRecords[chat.id] else { continue }
                    let recovered = recoverDraft(saved, draft, submissions[chat.id])
                    draftRecords[chat.id] = recovered
                    drafts[chat.id] = recovered.text
                    submissions.removeValue(forKey: chat.id)
                    put(saved, recovery: true)
                    _ = try await rpc(
                        "draft",
                        [
                            "chat_id": .string(chat.id), "text": .string(recovered.text),
                            "revision": .number(Double(recovered.revision)),
                        ])
                }
            }
        } catch {
            fail(error)
            offline = true
        }
    }

    /// Apply one runtime event from the service.
    public func handle(_ event: RuntimeEvent) {
        eventVersion += 1
        switch event.event {
        case "attempt":
            guard let attemptId = event.attemptId, let chatId = event.chatId,
                let requestId = event.requestId
            else { return }
            if let previous = events[attemptId], let status = previous.status,
                status != "running", event.status == "running"
            {
                return
            }
            events[attemptId] = event
            if let chat = cache[chatId] { cache[chatId] = applyAttempt(chat, event) }
            if event.status == "running" {
                active = Active(chatId: chatId, attemptId: attemptId, requestId: requestId)
                phase = event.phase
            }
        case "browser_control":
            if let control = event.control { browserControl = control }
        case "chat_mode":
            // An approved plan ends plan mode for the chat's next replies.
            guard let id = event.chatId, let mode = event.mode else { return }
            cache[id]?.summary.mode = mode
            if let index = chats.firstIndex(where: { $0.id == id }) { chats[index].mode = mode }
        case "idle":
            if active?.attemptId == event.attemptId { active = nil }
            phase = nil
        case "notice":
            error = event.message
        case "fatal", "disconnected":
            offline = true
            active = nil
            phase = nil
            for (id, saved) in events where saved.status == "running" {
                var interrupted = saved
                interrupted.status = "interrupted"
                interrupted.unsaved = true
                events[id] = interrupted
            }
            error =
                event.message
                ?? "The chat service disconnected. Your visible draft is retained. Reconnect to continue."
            for (id, chat) in cache {
                var chat = chat
                for m in chat.messages.indices {
                    for a in chat.messages[m].attempts.indices
                    where chat.messages[m].attempts[a].status == "running" {
                        chat.messages[m].attempts[a].status = "interrupted"
                        chat.messages[m].attempts[a].unsaved = true
                    }
                }
                cache[id] = chat
            }
        default:
            break
        }
    }

    // MARK: Chats

    public func select(_ id: String) async {
        selected = id
        do {
            try await loadChat(id)
            _ = try await rpc("preference", ["key": "selected_chat", "value": .string(id)])
        } catch {
            fail(error)
        }
    }

    public func newChat(folder: String? = nil) async {
        do {
            let chat = try await rpc(Chat.self, "create_chat", ["folder_id": folder.json])
            put(chat)
            await select(chat.id)
        } catch {
            fail(error)
        }
    }

    func saveDraft(_ id: String) async throws {
        timers.removeValue(forKey: id)?.cancel()
        // A send may have consumed this draft even if its acknowledgement was lost.
        if submissions[id] != nil {
            throw ServiceError("Reconnect to confirm the last send before saving this draft.")
        }
        if let draft = draftRecords[id] {
            _ = try await rpc(
                "draft",
                [
                    "chat_id": .string(id), "text": .string(draft.text),
                    "revision": .number(Double(draft.revision)),
                ])
        }
    }

    public func changeDraft(_ text: String) {
        guard let id = selected, drafts[id] != text else { return }
        let previous = draftRecords[id]
        draftRecords[id] = DraftRecord(
            text: text,
            revision: (previous?.revision ?? cache[id]?.summary.draftRevision ?? 0) + 1)
        drafts[id] = text
        timers[id]?.cancel()
        timers[id] = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled, let self else { return }
            do {
                try await self.saveDraft(id)
            } catch {
                self.error =
                    "Draft not saved. Keep a copy of your text and reconnect after checking disk space."
            }
        }
    }

    /// Save every draft now, before quitting or restarting.
    public func flushDrafts() async throws {
        for id in draftRecords.keys { try await saveDraft(id) }
    }

    public func updateChat(_ fields: [String: JSONValue]) async {
        guard let id = selected else { return }
        do {
            put(
                try await rpc(
                    Chat.self, "update_chat", ["chat_id": .string(id), "fields": .object(fields)]))
        } catch {
            fail(error)
        }
    }

    public func setModel(_ name: String) async { await updateChat(["model": .string(name)]) }

    /// Choose a thinking setting for the open chat's model; nil is the default ("on").
    public func setThinking(_ value: Thinking?, for model: String) async {
        guard let chat else { return }
        var thinking: [String: JSONValue] = [:]
        for (key, saved) in chat.summary.thinking { thinking[key] = saved?.json ?? .null }
        thinking[model] = value?.json ?? .null
        await updateChat(["thinking": .object(thinking)])
    }

    public func setMode(_ mode: ChatMode) async { await updateChat(["mode": .string(mode.rawValue)]) }

    public func rename(_ title: String) async { await updateChat(["title": .string(title)]) }

    public func move(toFolder folder: String?) async { await updateChat(["folder_id": folder.json]) }

    // MARK: Replies

    /// Whether the open chat can send its draft now.
    public var canSend: Bool {
        guard let chat, model != nil, !offline, !sending, !importing, active == nil else {
            return false
        }
        let pending = chat.attachments.filter { $0.messageId == nil }
        if pending.contains(where: { !$0.isDocument }) && model?.vision != true { return false }
        return !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !pending.isEmpty
    }

    public func send(retry: Bool = false) async {
        guard let id = selected, active == nil, !sendLock, !importing else { return }
        sendLock = true
        sending = true
        error = nil
        defer {
            sendLock = false
            sending = false
        }
        do {
            try await saveDraft(id)
            let requestId = UUID().uuidString.lowercased()
            if !retry {
                submissions[id] = Submission(
                    requestId: requestId, text: draftRecords[id]?.text ?? "",
                    revision: draftRecords[id]?.revision ?? 0)
            }
            let result = try await rpc(
                "send",
                ["chat_id": .string(id), "retry": .bool(retry), "request_id": .string(requestId)])
            let chat = try result["chat"].orThrow().decode(Chat.self)
            submissions.removeValue(forKey: id)
            if !retry {
                draftRecords[id] = DraftRecord(
                    text: chat.summary.draft, revision: chat.summary.draftRevision)
                drafts[id] = chat.summary.draft
            }
            put(chat)
            active = reconcileActive(try? result["active"]?.decode(Active.self), events)
            if let models = try? result["models"]?.decode([LocalModel].self) {
                self.models = models
            }
        } catch {
            fail(error)
            do {
                let saved = try await rpc(Chat.self, "chat", ["chat_id": .string(id)])
                if let draft = draftRecords[id] {
                    let recovered = recoverDraft(saved, draft, submissions[id])
                    draftRecords[id] = recovered
                    drafts[id] = recovered.text
                }
                submissions.removeValue(forKey: id)
                put(saved)
                Task { await refreshModels() }
            } catch {
                offline = true
            }
        }
    }

    public func stop() async {
        do { _ = try await rpc("stop") } catch { fail(error) }
    }

    /// Answer what the running reply waits for; true when the service accepted it.
    @discardableResult
    public func respond(attempt: Attempt, record: ActivityRecord, reply: Reply) async -> Bool {
        var params: [String: JSONValue] = [
            "chat_id": .string(attempt.chatId), "attempt_id": .string(attempt.id),
            "interaction_id": .string(record.id), "kind": .string(record.kind),
        ]
        params.merge(reply.fields) { _, new in new }
        do {
            _ = try await rpc("respond", params)
            return true
        } catch {
            fail(error)
            return false
        }
    }

    /// Take the browser over (Vikram's actions wait) or hand it back.
    @discardableResult
    public func controlBrowser(_ control: BrowserControl) async -> Bool {
        do {
            let result = try await rpc("browser_control", ["control": .string(control.rawValue)])
            if let value = result["control"]?.string, let control = BrowserControl(rawValue: value)
            {
                browserControl = control
            }
            return true
        } catch {
            fail(error)
            return false
        }
    }

    // MARK: Folders and deletion

    @discardableResult
    public func manage(_ method: String, _ params: [String: JSONValue]) async -> Bool {
        do {
            _ = try await rpc(method, params)
            try await snapshot()
            return true
        } catch {
            fail(error)
            return false
        }
    }

    public func createFolder(_ name: String) async -> Bool {
        await manage("create_folder", ["name": .string(name)])
    }

    public func renameFolder(_ id: String, to name: String) async -> Bool {
        await manage("rename_folder", ["folder_id": .string(id), "name": .string(name)])
    }

    public func deleteFolder(_ id: String) async -> Bool {
        await manage("delete_folder", ["folder_id": .string(id)])
    }

    @discardableResult
    public func deleteChat(_ id: String) async -> Bool {
        guard await manage("delete_chat", ["chat_id": .string(id)]) else { return false }
        timers.removeValue(forKey: id)?.cancel()
        draftRecords.removeValue(forKey: id)
        cache.removeValue(forKey: id)
        guard let state = try? await snapshot() else { return true }
        if selected == id {
            selected = state.chats.first?.id
            if let next = selected { _ = try? await loadChat(next) }
        }
        return true
    }

    // MARK: Attachments

    /// Import files the person chose, dropped, or pasted into the open chat.
    public func importFiles(_ urls: [URL]) async {
        await importAttachments(
            urls.map { url in
                {
                    try Self.readAttachment(url)
                }
            }, total: urls.count)
    }

    /// Import pasted data, such as an image copied from another app.
    public func importData(_ data: Data, name: String) async {
        await importAttachments(
            [
                {
                    guard data.count <= Self.maxFileSize else {
                        throw ServiceError("Files must be no larger than 10 MiB.")
                    }
                    return (name, data)
                }
            ], total: 1)
    }

    nonisolated static func readAttachment(_ url: URL) throws -> (String, Data) {
        let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
        guard values?.isRegularFile == true else {
            throw ServiceError("Choose a regular file.")
        }
        guard (values?.fileSize ?? 0) <= maxFileSize else {
            throw ServiceError("Files must be no larger than 10 MiB.")
        }
        guard let handle = try? FileHandle(forReadingFrom: url),
            let data = try? handle.read(upToCount: maxFileSize + 1)
        else {
            throw ServiceError("The selected file cannot be read.")
        }
        guard data.count <= maxFileSize else {
            throw ServiceError("Files must be no larger than 10 MiB.")
        }
        return (url.lastPathComponent, data)
    }

    private func importAttachments(
        _ sources: [@Sendable () throws -> (String, Data)], total: Int
    ) async {
        guard let id = selected, !importing, !sendLock else { return }
        importing = true
        defer { importing = false }
        var errors: [String] = []
        for source in sources.prefix(Self.maxFiles) {
            do {
                let (name, data) = try await Task.detached { try source() }.value
                _ = try await rpc(
                    "import_attachment",
                    [
                        "chat_id": .string(id), "name": .string(name),
                        "data": .string(data.base64EncodedString()),
                    ])
            } catch {
                errors.append(
                    (error as? ServiceError)?.message ?? "This file could not be imported.")
            }
        }
        if total > Self.maxFiles { errors.append("Attach up to four files per message.") }
        do { try await loadChat(id) } catch { fail(error) }
        var seen = Set<String>()
        let unique = errors.filter { seen.insert($0).inserted }
        if !unique.isEmpty { error = unique.joined(separator: " ") }
    }

    public func removeAttachment(_ attachmentId: String) async {
        guard let id = selected else { return }
        do {
            _ = try await rpc(
                "remove_attachment", ["chat_id": .string(id), "attachment_id": .string(attachmentId)])
            try await loadChat(id)
        } catch {
            fail(error)
        }
    }

    /// An image attachment's bytes, for display.
    public func imageData(_ attachment: Attachment) async -> Data? {
        guard
            let url = try? await rpc(
                "image",
                ["chat_id": .string(attachment.chatId), "attachment_id": .string(attachment.id)]
            ).string,
            let comma = url.firstIndex(of: ",")
        else { return nil }
        return Data(base64Encoded: String(url[url.index(after: comma)...]))
    }

    // MARK: Preferences

    public func preference(_ key: String, _ value: JSONValue) async {
        preferences[key] = value
        do { _ = try await rpc("preference", ["key": .string(key), "value": value]) } catch {
            fail(error)
        }
    }

    public func reconnect() async { await boot(reconnect: true) }

    /// Show a chat a scheduled run was copied into.
    public func adopt(_ chat: Chat) async {
        put(chat)
        await select(chat.id)
    }
}

extension Optional where Wrapped == JSONValue {
    func orThrow() throws -> JSONValue {
        guard let self else { throw ServiceError("The chat service sent an unexpected reply.") }
        return self
    }
}
