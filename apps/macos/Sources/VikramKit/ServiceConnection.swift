import Foundation

/// A problem the person can read: the service's own message, or why it is unreachable.
public struct ServiceError: LocalizedError, Sendable, Equatable {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

/// The private chat service: the bundled Python runtime, spoken to in versioned
/// JSON lines over its standard input and output.
///
/// Each start is a new generation; replies and events from an older generation
/// are dropped. A command waits up to two minutes, after which the service is
/// stopped so a reconnect can recover saved work.
public final class ServiceConnection: @unchecked Sendable {
    public typealias EventHandler = @Sendable (JSONValue) -> Void
    public typealias BrowserHandler = @Sendable (_ callId: String, _ call: JSONValue) -> Void

    private struct Running {
        let process: Process
        let input: FileHandle
        let generation: UInt64
    }

    private struct Pending {
        let generation: UInt64
        let continuation: CheckedContinuation<JSONValue, Error>
        var timeout: Task<Void, Never>?
    }

    private let lock = NSLock()
    private var running: Running?
    private var pending: [String: Pending] = [:]
    private var serial: UInt64 = 0
    private var generation: UInt64 = 0
    private var queued = 0
    private let writes = DispatchQueue(label: "dev.raniendu.vikram.service-input")
    private let appVersion: String
    private let launcher: @Sendable () throws -> Process

    /// Runtime events for the window: replies, notices, and disconnection.
    public var onEvent: EventHandler?
    /// Browser actions for the app's panel; never shown to the window.
    public var onBrowser: BrowserHandler?

    static let replyTimeout: Duration = .seconds(120)
    static let maxCommand = 15 * 1024 * 1024
    static let maxLine = 64 * 1024 * 1024

    public init(appVersion: String, launcher: (@Sendable () throws -> Process)? = nil) {
        self.appVersion = appVersion
        self.launcher = launcher ?? ServiceConnection.defaultProcess
    }

    /// The bundled service beside the app's executable, or in a development build,
    /// the checkout's Python environment.
    @Sendable public static func defaultProcess() throws -> Process {
        let process = Process()
        let bundled = Bundle.main.executableURL?.deletingLastPathComponent()
            .appendingPathComponent("vikram-sidecar")
        if let bundled, FileManager.default.isExecutableFile(atPath: bundled.path) {
            process.executableURL = bundled
            return process
        }
        #if DEBUG
            let repository = URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent().deletingLastPathComponent()
                .deletingLastPathComponent().deletingLastPathComponent()
                .deletingLastPathComponent()
            let python = repository.appendingPathComponent(".venv/bin/python")
            if FileManager.default.isExecutableFile(atPath: python.path) {
                process.executableURL = python
                process.arguments = ["-m", "vikram.desktop"]
                process.currentDirectoryURL = repository
                return process
            }
        #endif
        throw ServiceError("Cannot start the bundled chat service. Reconnect or rebuild Vikram.")
    }

    private func launch() throws {
        lock.lock()
        defer { lock.unlock() }
        if running != nil { return }
        generation += 1
        let current = generation
        let process = try launcher()
        var environment = ProcessInfo.processInfo.environment
        // The service may be newer than this window after an update replaced
        // the app's files; it compares against the version the window runs.
        environment["VIKRAM_APP_VERSION"] = appVersion
        process.environment = environment
        let input = Pipe()
        let output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            throw ServiceError(
                "Cannot start the bundled chat service. Reconnect or rebuild Vikram.")
        }
        running = Running(
            process: process, input: input.fileHandleForWriting, generation: current)
        let reader = output.fileHandleForReading
        let thread = Thread { [weak self] in self?.read(reader, generation: current) }
        thread.name = "Vikram service output"
        thread.start()
    }

    private func read(_ handle: FileHandle, generation current: UInt64) {
        var buffer = Data()
        reading: while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            buffer.append(chunk)
            while let newline = buffer.firstIndex(of: 0x0A) {
                let line = buffer[buffer.startIndex..<newline]
                buffer = Data(buffer[buffer.index(after: newline)...])
                guard let message = try? JSONDecoder().decode(JSONValue.self, from: line),
                    message["v"] == .number(1), isCurrent(current)
                else { break reading }
                route(message)
            }
            if buffer.count > Self.maxLine { break }
        }
        if shutDown(generation: current) {
            onEvent?(["v": 1, "event": "disconnected"])
        }
    }

    private func isCurrent(_ value: UInt64) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return generation == value
    }

    private func route(_ message: JSONValue) {
        if let id = message["id"]?.string {
            lock.lock()
            let entry = pending.removeValue(forKey: id)
            lock.unlock()
            guard let entry else { return }
            entry.timeout?.cancel()
            if let error = message["error"]?.string {
                entry.continuation.resume(throwing: ServiceError(error))
            } else {
                entry.continuation.resume(returning: message["result"] ?? .null)
            }
        } else if message["event"]?.string == "browser" {
            if let callId = message["call_id"]?.string {
                onBrowser?(callId, message["call"] ?? .null)
            }
        } else {
            onEvent?(message)
        }
    }

    /// Send one command and wait for its result. Only `hello` starts a service.
    public func call(_ method: String, _ params: [String: JSONValue] = [:]) async throws
        -> JSONValue
    {
        if method == "hello" { try launch() }
        let id = lock.withLock {
            serial += 1
            return "r\(serial)"
        }
        let command: JSONValue = [
            "v": 1, "id": .string(id), "method": .string(method), "params": .object(params),
        ]
        let line = try command.encoded() + Data([0x0A])
        if line.count > Self.maxCommand {
            throw ServiceError("Image or command exceeds the size limit.")
        }
        return try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            guard let running else {
                lock.unlock()
                continuation.resume(
                    throwing: ServiceError("The chat service is disconnected. Choose Reconnect."))
                return
            }
            if queued >= 8 {
                lock.unlock()
                continuation.resume(
                    throwing: ServiceError(
                        "The chat service is busy or disconnected. Try again or reconnect."))
                return
            }
            let current = running.generation
            var entry = Pending(generation: current, continuation: continuation)
            entry.timeout = Task { [weak self] in
                try? await Task.sleep(for: Self.replyTimeout)
                guard !Task.isCancelled else { return }
                self?.timedOut(id: id, generation: current)
            }
            pending[id] = entry
            queued += 1
            lock.unlock()
            let input = running.input
            writes.async { [weak self] in
                // Writes happen off the caller; a stopped service fails them quietly.
                try? input.write(contentsOf: line)
                guard let self else { return }
                self.lock.lock()
                self.queued -= 1
                self.lock.unlock()
            }
        }
    }

    private func timedOut(id: String, generation current: UInt64) {
        lock.lock()
        let entry = pending.removeValue(forKey: id)
        lock.unlock()
        guard let entry else { return }
        entry.continuation.resume(
            throwing: ServiceError(
                "The chat service stopped responding and was closed. Reconnect to recover saved work."
            ))
        DispatchQueue.global().async { [weak self] in
            if self?.shutDown(generation: current) == true {
                self?.onEvent?(["v": 1, "event": "disconnected"])
            }
        }
    }

    /// Stop the service of `generation` (any, when nil); true if one stopped.
    @discardableResult
    private func shutDown(generation expected: UInt64?) -> Bool {
        lock.lock()
        guard let current = running, expected == nil || current.generation == expected else {
            lock.unlock()
            return false
        }
        running = nil
        generation += 1
        let failed = pending.filter { $0.value.generation == current.generation }
        for id in failed.keys { pending.removeValue(forKey: id) }
        lock.unlock()
        for entry in failed.values {
            entry.timeout?.cancel()
            entry.continuation.resume(
                throwing: ServiceError("The chat service disconnected. Reconnect to continue."))
        }
        // Closing input lets the service finish saving, then exit on its own.
        try? current.input.close()
        let deadline = Date().addingTimeInterval(2)
        while current.process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if current.process.isRunning {
            current.process.terminate()
            current.process.waitUntilExit()
        }
        return true
    }

    /// Stop the service; its library lock is released when this returns.
    public func shutDown() {
        shutDown(generation: nil)
    }

    /// Stop the running service and start a new one.
    public func reconnect() async throws -> JSONValue {
        await Task.detached { [self] in shutDown() }.value
        return try await call("hello")
    }
}
