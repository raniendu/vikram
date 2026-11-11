import Foundation
import Testing

@testable import VikramKit

@Suite struct BotDecodingTests {
  @Test func homeKeepsFindingsLivenessAndExplicitCloudPermission() throws {
    let value: JSONValue = [
      "needs_you": [
        [
          "id": "f1", "task_id": "b1", "run_id": "r1", "key": "fee",
          "kind": "material-change", "severity": "attention", "title": "Fee changed",
          "state": "new", "bot_name": "Immigration", "sources": ["https://example.gov"],
        ]
      ],
      "material": [], "stale": [],
      "bots": [
        [
          "id": "b1", "name": "Immigration", "mandate": "Watch official rule changes.",
          "schedule": ["repeat": "weekly", "days": ["mon"], "time": "08:00"],
          "cloud": false,
          "last_run": [
            "id": "r1", "status": "complete", "started": 100,
            "outcome": "needs-attention",
          ],
        ]
      ],
    ]
    let home = try value.decode(Home.self)
    #expect(!home.isCalm)
    #expect(home.needsYou.first?.runId == "r1")
    #expect(home.needsYou.first?.count == 1)
    #expect(home.needsYou.first?.held == false)
    #expect(home.bots.first?.cloud == false)
    #expect(home.bots.first?.lastRun?.outcome == "needs-attention")
    #expect(home.bots.first?.model == nil)
    #expect(home.bots.first?.budgetMinutes == 10)
    #expect(Home.empty.isCalm)
  }

  @Test func outcomesDoNotReplaceProcessStatusAndConfigRetainsFields() throws {
    let value: JSONValue = [
      "id": "r1", "task_id": "b1", "trigger": "manual", "started": 100,
      "status": "complete", "outcome": "blocked", "report": "Source was unavailable.",
      "prompt": "Check now.", "content": "Evidence unavailable.",
    ]
    let run = try value.decode(TaskRun.self)
    #expect(run.summary.status == "complete")
    #expect(run.summary.outcome == "blocked")
    #expect(run.summary.report == "Source was unavailable.")
    #expect(run.attempt.content == "Evidence unavailable.")
    let botValue: JSONValue = [
      "id": "b1", "name": "Watcher", "schedule": ["repeat": "daily", "time": "08:00"],
      "config": ["version": 1, "cloud": true, "materiality": ["keywords": ["visa"]]],
      "findings": [], "runs": [value],
    ]
    let bot = try botValue.decode(Bot.self)
    #expect(bot.config?["materiality"]?["keywords"] == ["visa"])
    #expect(bot.runs?.first?.outcome == "blocked")
  }
}

private let botStub = """
  import json, sys
  bot = {"id":"b1","name":"Watcher","mandate":"Watch.","enabled":True,
         "schedule":{"repeat":"daily","time":"08:00"},"cloud":False,
         "config":{"version":1,"name":"Watcher","cloud":False},"runs":[],"findings":[]}
  finding = {"id":"f1","task_id":"b1","key":"fee","kind":"material-change",
             "severity":"attention","title":"Fee","state":"new","bot_name":"Watcher"}
  for line in sys.stdin:
      command = json.loads(line)
      method, p = command["method"], command.get("params", {})
      if method == "home":
          result = {"needs_you":[finding] if finding["state"] in ("new","seen") else [],
                    "material":[],"stale":[],"bots":[bot]}
      elif method == "bots":
          result = {"bots":[bot],"background":"unavailable"}
      elif method == "bot_templates":
          result = {"templates":[{"id":"topic-watcher","title":"Topic Watcher",
                                  "summary":"Reports material developments.","available":True}]}
      elif method == "bot": result = bot
      elif method == "update_bot":
          assert p["task_id"] == "b1"
          bot.update(p["fields"])
          result = bot
      elif method == "create_bot":
          if p["fields"].get("name") == "fail":
              print(json.dumps({"v":1,"id":command["id"],"error":"Invalid bot."}), flush=True)
              continue
          bot.update(p["fields"])
          result = bot
      elif method == "finding":
          finding["state"] = p["state"]
          result = finding
      elif method == "finding_feedback":
          finding["state"] = "dismissed" if p["verdict"] == "not-material" else "seen"
          result = finding
      elif method == "export_bot": result = bot["config"]
      else: result = None
      print(json.dumps({"v":1,"id":command["id"],"result":result}), flush=True)
  """

@MainActor @Suite(.serialized) struct BotControllerTests {
  @Test func controllerLoadsNavigatesUpdatesFindingsAndReportsErrors() async throws {
    let service = ServiceConnection(appVersion: "test") {
      let process = Process()
      process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
      process.arguments = ["-c", botStub]
      return process
    }
    defer { service.shutDown() }
    _ = try await service.call("hello")
    let controller = BotController(service: service)
    try await controller.refresh()
    #expect(controller.loaded)
    #expect(controller.attentionCount == 1)
    #expect(controller.templates.first?.id == "topic-watcher")
    await controller.openBot("b1")
    #expect(controller.bot?.name == "Watcher")
    await controller.setPaused("b1", true)
    #expect(controller.bot?.enabled == false)
    await controller.setFinding("f1", state: "snoozed", until: .now.addingTimeInterval(3600))
    #expect(controller.attentionCount == 0)
    await controller.feedback("f1", verdict: "material")
    #expect(controller.attentionCount == 1)
    await controller.feedback("f1", verdict: "not-material")
    #expect(controller.home.isCalm)
    let exported = await controller.export("b1")
    #expect(exported?["version"] == 1)
    let failed = await controller.create(fields: ["name": "fail"])
    #expect(failed == nil)
    #expect(controller.error == "Invalid bot.")
    #expect(!controller.working)
  }
}
