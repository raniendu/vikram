import Foundation
import Testing

@testable import VikramKit

@Suite struct WorkbenchDecodingTests {
    @Test func providerMetadataKeepsQualifiedIDsAndLocalAliases() throws {
        let cloud: JSONValue = [
            "name": "openrouter:example/reasoner", "provider": "openrouter",
            "display_name": "Example Reasoner", "available": true, "thinking": ["low", "high"],
            "context_length": 64000,
        ]
        let model = try cloud.decode(LocalModel.self)
        #expect(model.isCloud)
        #expect(model.providerLabel == "OpenRouter")
        #expect(model.displayName == "Example Reasoner")
        #expect(model.contextLength == 64000)
        #expect(LocalModel(name: "gemma4:26b").matches("ollama:gemma4:26b"))
        #expect(!model.matches("example/reasoner"))
    }

    @Test func sharedStatusKeepsQueueKindsAndUnknownContext() throws {
        let value: JSONValue = [
            "workspace": "/tmp/project", "trusted": false, "provider": "ollama",
            "cloud_enabled": false, "credentials": nil, "context": ["limit": nil],
            "queue": [["id": "q1", "text": "Inspect first", "kind": "steer"]],
            "resources": [:], "mcp": [], "extensions": [],
            "default_model": "ollama:local:1",
        ]
        let status = try value.decode(WorkbenchStatus.self)
        #expect(status.workspace == "/tmp/project")
        #expect(!status.trusted)
        #expect(status.context["limit"] == .null)
        #expect(status.queue.first?.kind == .steer)
        #expect(status.credentials == nil)
        #expect(status.defaultModel == "ollama:local:1")
    }

    @Test(arguments: ["coding", "mcp", "extension"])
    func hostApprovalsPreserveSummaryAndDiff(_ kind: String) throws {
        let value: JSONValue = [
            "kind": .string(kind), "id": "tool1", "status": "waiting",
            "summary": "Update a file", "reason": "Outside workspace", "url": "/tmp/file.py",
            "diff": "-old\n+new", "output": "Ready for approval",
        ]
        let record = try value.decode(ActivityRecord.self)
        guard case .tool(let tool) = record else {
            Issue.record("Expected a visible tool approval")
            return
        }
        #expect(tool.title == "Update a file")
        #expect(tool.diff == "-old\n+new")
        #expect(tool.reason == "Outside workspace")
        #expect(record.kind == kind)
        #expect(Reply.dismiss.fields["decision"] == "dismiss")
    }
}

private let workbenchStub = """
    import json, sys
    queue = []
    chat = {"id":"c1","title":"Chat","model":"local:1","messages":[],"draft":"","draft_revision":0}
    def status():
        return {"workspace":None,"trusted":False,"provider":"ollama","cloud_enabled":False,
                "credentials":None,"context":{},"queue":queue,"resources":{},"mcp":[],"extensions":[]}
    for line in sys.stdin:
        command = json.loads(line)
        method, params = command["method"], command.get("params", {})
        if method in ("hello", "snapshot"):
            result = {"protocol":1,"chats":[chat],"folders":[],"preferences":{},"active":None,"models":[]}
        elif method == "refresh":
            result = {"models":[{"name":"local:1","available":True}]}
        elif method == "chat":
            result = chat
        elif method == "workbench":
            action = params["action"]
            if action == "queue":
                if params["value"] == "fail":
                    print(json.dumps({"v":1,"id":command["id"],"error":"Queue unavailable."}), flush=True)
                    continue
                assert params["chat_id"] == "c1"
                queue.append({"id":"q1","text":params["value"],"kind":params["options"]["kind"]})
            elif action == "queue_remove":
                queue = [entry for entry in queue if entry["id"] != params["value"]]
            result = status()
        else:
            result = None
        print(json.dumps({"v":1,"id":command["id"],"result":result}), flush=True)
    """

@MainActor @Suite(.serialized) struct WorkbenchControllerTests {
    private func service() -> ServiceConnection {
        ServiceConnection(appVersion: "test") {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = ["-c", workbenchStub]
            return process
        }
    }

    @Test func queueActionsUseSelectedChatAndClearOnlyAcceptedDrafts() async throws {
        let connection = service()
        defer { connection.shutDown() }
        let chats = ChatController(service: connection)
        let workbench = WorkbenchController(service: connection, chats: chats)
        await chats.boot()
        chats.changeDraft("Inspect first")
        #expect(await workbench.queueDraft(kind: .steer))
        #expect(chats.draft.isEmpty)
        #expect(workbench.status?.queue.first?.text == "Inspect first")
        #expect(workbench.status?.queue.first?.kind == .steer)
        _ = await workbench.perform("queue_remove", value: "q1")
        #expect(workbench.status?.queue.isEmpty == true)
        chats.changeDraft("fail")
        #expect(await !workbench.queueDraft(kind: .followup))
        #expect(chats.draft == "fail")
        #expect(workbench.error == "Queue unavailable.")
    }
}
