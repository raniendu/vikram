import Foundation
import Observation

/// Scheduled tasks, the open task and run, and the actions the window offers.
///
/// Runs happen in the background (even with the app closed); the service tells
/// the window when tasks or runs change, and this reloads what is open.
@MainActor @Observable
public final class ScheduleController {
    public private(set) var tasks: [ScheduledTask] = []
    /// on, off, unavailable, or failed.
    public private(set) var background = "unavailable"
    public private(set) var task: ScheduledTask?
    public private(set) var run: TaskRun?
    public var error: String?
    public private(set) var working = false

    @ObservationIgnored private let service: ServiceConnection
    @ObservationIgnored private var openTaskId: String?
    @ObservationIgnored private var openRunId: String?
    @ObservationIgnored private var pendingReload: Task<Void, Never>?

    public init(service: ServiceConnection) {
        self.service = service
    }

    private func fail(_ error: Error) {
        self.error =
            (error as? ServiceError)?.message ?? "This action could not finish. Try again."
    }

    public func refresh() async throws {
        let list = try await service.call("schedules").decode(ScheduleList.self)
        tasks = list.tasks
        background = list.background
        if let taskId = openTaskId {
            if list.tasks.contains(where: { $0.id == taskId }) {
                task = try await service.call("schedule", ["task_id": .string(taskId)])
                    .decode(ScheduledTask.self)
            } else {
                openTaskId = nil
                openRunId = nil
                task = nil
                run = nil
            }
        }
        if let runId = openRunId {
            do {
                let loaded = try await service.call("schedule_run", ["run_id": .string(runId)])
                    .decode(TaskRun.self)
                if openRunId == runId { run = loaded }
            } catch {
                if openRunId == runId {
                    run = nil
                    fail(error)
                }
            }
        }
    }

    /// Several changes arrive together while a run streams: reload once.
    public func changed() {
        guard pendingReload == nil else { return }
        pendingReload = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(150))
            guard let self else { return }
            self.pendingReload = nil
            try? await self.refresh()
        }
    }

    public func openTask(_ id: String?) async {
        openTaskId = id
        openRunId = nil
        run = nil
        guard let id else {
            task = nil
            return
        }
        do {
            task = try await service.call("schedule", ["task_id": .string(id)])
                .decode(ScheduledTask.self)
        } catch {
            fail(error)
        }
    }

    public func openRun(_ id: String?) async {
        openRunId = id
        run = nil
        error = nil
        guard let id else { return }
        do {
            let loaded = try await service.call("schedule_run", ["run_id": .string(id)])
                .decode(TaskRun.self)
            if openRunId == id { run = loaded }
        } catch {
            if openRunId == id { fail(error) }
        }
    }

    @discardableResult
    private func act(_ body: () async throws -> JSONValue) async -> JSONValue? {
        working = true
        error = nil
        defer { working = false }
        do {
            let result = try await body()
            try await refresh()
            return result
        } catch {
            fail(error)
            return nil
        }
    }

    /// Create a task, or change one; true when saved.
    public func save(
        name: String, prompt: String, schedule: ScheduleTime, model: String, taskId: String?
    ) async -> Bool {
        let fields: JSONValue = [
            "name": .string(name), "prompt": .string(prompt), "schedule": schedule.fields,
            "model": .string(model),
        ]
        return await act {
            if let taskId {
                try await service.call(
                    "update_schedule", ["task_id": .string(taskId), "fields": fields])
            } else {
                try await service.call("create_schedule", ["fields": fields])
            }
        } != nil
    }

    public func setPaused(_ id: String, _ paused: Bool) async {
        await act {
            try await service.call(
                "update_schedule", ["task_id": .string(id), "fields": ["enabled": .bool(!paused)]])
        }
    }

    public func runNow(_ id: String) async {
        await act { try await service.call("run_schedule", ["task_id": .string(id)]) }
    }

    public func remove(_ id: String) async -> Bool {
        await act { try await service.call("delete_schedule", ["task_id": .string(id)]) } != nil
    }

    /// Copy a finished run into a new chat, so the person can follow up.
    public func continueInChat(_ runId: String) async -> Chat? {
        guard let value = await act({ try await service.call("open_run", ["run_id": .string(runId)]) })
        else { return nil }
        return try? value.decode(Chat.self)
    }
}

/// Updates: the service checks when the window opens and installs a newer
/// release for the app and CLI in the background; an update from
/// `vikram update` shows up too. The window then offers to restart.
@MainActor @Observable
public final class UpdateController {
    public private(set) var status: UpdateStatus?
    private var dismissed: String?
    @ObservationIgnored private let service: ServiceConnection

    public init(service: ServiceConnection) {
        self.service = service
    }

    /// The version a restart would start, unless the person set it aside.
    public var offer: String? {
        guard let status, status.state == "ready", status.version != dismissed else { return nil }
        return status.version
    }

    public func dismiss() { dismissed = status?.version }

    public func handle(_ event: JSONValue) {
        if let status = try? event.decode(UpdateStatus.self) { self.status = status }
    }

    public func load() async {
        if let value = try? await service.call("check_update").decode(UpdateStatus.self),
            status == nil
        {
            status = value
        }
    }

    public func check() async {
        if let value = try? await service.call("check_update").decode(UpdateStatus.self) {
            status = value
        }
    }

    public var text: String {
        guard let status else { return "" }
        switch status.state {
        case "checking": return "Checking for updates…"
        case "installing": return "Updating to \(status.version ?? "the new version")…"
        case "current": return "Up to date."
        case "ready": return "\(status.version ?? "An update") is installed."
        case "failed": return status.error ?? "Could not check for updates."
        default: return ""
        }
    }
}
