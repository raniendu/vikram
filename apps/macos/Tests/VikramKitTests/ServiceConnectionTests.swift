import Foundation
import Testing

@testable import VikramKit

/// A stand-in service: answers `hello`, fails `boom`, emits an event for `ping`,
/// and exits on `quit`, speaking the same JSON lines as the real one.
private let stub = """
    import json, sys
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd["method"] == "hello":
            print(json.dumps({"v": 1, "id": cmd["id"], "result": {"protocol": 1}}), flush=True)
        elif cmd["method"] == "boom":
            print(json.dumps({"v": 1, "id": cmd["id"], "error": "It broke."}), flush=True)
        elif cmd["method"] == "ping":
            print(json.dumps({"v": 1, "event": "notice", "message": "pong"}), flush=True)
            print(json.dumps({"v": 1, "event": "browser", "call_id": "c1", "call": {"action": "look"}}), flush=True)
            print(json.dumps({"v": 1, "id": cmd["id"], "result": None}), flush=True)
        elif cmd["method"] == "quit":
            sys.exit(0)
    """

private func service() -> ServiceConnection {
    ServiceConnection(appVersion: "1.2.3") {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-c", stub]
        return process
    }
}

private final class Inbox: @unchecked Sendable {
    private let lock = NSLock()
    private var items: [JSONValue] = []
    func add(_ item: JSONValue) { lock.withLock { items.append(item) } }
    var all: [JSONValue] { lock.withLock { items } }
}

@Suite(.serialized) struct ServiceConnectionTests {
    @Test func commandsNeedAStartedService() async {
        await #expect(throws: ServiceError.self) { try await service().call("snapshot") }
    }

    @Test func answersErrorsEventsAndBrowserCalls() async throws {
        let connection = service()
        let events = Inbox()
        let browser = Inbox()
        connection.onEvent = { events.add($0) }
        connection.onBrowser = { id, call in browser.add(["id": .string(id), "call": call]) }
        #expect(try await connection.call("hello")["protocol"] == 1)
        await #expect(throws: ServiceError("It broke.")) { try await connection.call("boom") }
        _ = try await connection.call("ping")
        #expect(events.all.first?["message"] == "pong")
        #expect(browser.all == [["id": "c1", "call": ["action": "look"]]])
        connection.shutDown()
    }

    @Test func reportsADisconnectAndFailsLaterCommands() async throws {
        let connection = service()
        let events = Inbox()
        connection.onEvent = { events.add($0) }
        _ = try await connection.call("hello")
        _ = try? await connection.call("quit")
        for _ in 0..<50 where events.all.isEmpty {
            try await Task.sleep(for: .milliseconds(50))
        }
        #expect(events.all.last?["event"] == "disconnected")
        await #expect(throws: ServiceError.self) { try await connection.call("snapshot") }
    }
}
