import Foundation
import Testing

@testable import VikramKit

private let recoveryStub = """
  import json, sys, threading
  mode = sys.argv[1]
  created, template_calls = 0, 0
  output = threading.Lock()
  def bot(identifier):
      return {"id":identifier,"name":identifier,"mandate":"Watch.",
              "schedule":{"repeat":"daily","time":"09:00"},"cloud":False}
  def reply(command, result=None, error=None):
      value = {"v":1,"id":command["id"]}
      value.update({"error":error} if error else {"result":result})
      with output:
          print(json.dumps(value), flush=True)
  for line in sys.stdin:
      command = json.loads(line)
      method, p = command["method"], command.get("params", {})
      if method == "hello": reply(command, {"protocol":1})
      elif method == "home":
          if mode == "home-after-create" and created:
              reply(command, error="Home unavailable.")
          else: reply(command, {"needs_you":[],"material":[],"stale":[],"bots":[]})
      elif method == "bots": reply(command, {"bots":[bot("fast"),bot("slow")],"background":"unavailable"})
      elif method == "bot_templates":
          template_calls += 1
          if mode == "templates-once" and template_calls == 1:
              reply(command, error="Templates unavailable.")
          else:
              reply(command, {"templates":[
                  {"id":"page-watcher","title":"Page Watcher","summary":"Watch pages.","available":True,
                   "defaults":{"mandate":"Watch pages.","schedule":{"repeat":"daily","time":"09:00"}}},
                  {"id":"blocked","title":"Blocked","summary":"Not ready.","available":False,"reason":"Sandbox required."}]})
      elif method == "create_bot":
          created += 1
          reply(command, bot("created"))
      elif method in ("bot", "schedule_run"):
          identifier = p.get("task_id", p.get("run_id"))
          if identifier == "missing": reply(command, error="Not found.")
          else:
              result = bot(identifier) if method == "bot" else {
                  "id":identifier,"task_id":"b1","trigger":"manual","started":100,
                  "status":"complete","prompt":"Check.","content":identifier}
              if identifier == "slow": threading.Timer(0.2, reply, args=(command,result)).start()
              else: reply(command, result)
      elif method == "inspection": reply(command, {"created":created})
      else: reply(command)
  """

@MainActor @Suite(.serialized) struct BotRecoveryTests {
  private func service(_ mode: String = "normal") async throws -> ServiceConnection {
    let service = ServiceConnection(appVersion: "test") {
      let process = Process()
      process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
      process.arguments = ["-c", recoveryStub, mode]
      return process
    }
    _ = try await service.call("hello")
    return service
  }

  @Test func templateFailuresAreVisibleAndRetryableWithoutHidingSavedBots() async throws {
    let connection = try await service("templates-once")
    defer { connection.shutDown() }
    let controller = BotController(service: connection)
    try await controller.refresh()
    #expect(controller.loaded && controller.bots.count == 2)
    #expect(controller.templates.isEmpty)
    #expect(controller.templateError == "Templates unavailable.")
    #expect(controller.draft() == nil)
    #expect(controller.error != nil)
    try await controller.refresh()
    #expect(controller.templateError == nil)
    let newBot = controller.draft()
    #expect(newBot?.name == "")
    #expect(newBot?.mandate == "")
    #expect(newBot?.fields["schedule"] == ["repeat": "daily", "time": "09:00"])
    #expect(newBot?.cloud == false)
    let draft = controller.draft(templateId: "page-watcher")
    #expect(draft?.repeatKind == "daily")
    #expect(controller.draft(templateId: "blocked") == nil)
    #expect(controller.error == "Sandbox required.")
  }

  @Test func acknowledgedCreationSurvivesRefreshFailureWithoutInvitingDuplicates() async throws {
    let connection = try await service("home-after-create")
    defer { connection.shutDown() }
    let controller = BotController(service: connection)
    try await controller.refresh()
    let created = await controller.create(fields: ["name": "Watch"])
    #expect(created?.id == "created")
    #expect(!controller.working && !controller.loading)
    #expect(controller.error?.contains("action completed") == true)
    #expect(try await connection.call("inspection")["created"] == 1)
  }

  @Test func lateDetailCannotReplaceTheNewSelectionAndFailuresDoNotSpinForever() async throws {
    let connection = try await service()
    defer { connection.shutDown() }
    let controller = BotController(service: connection)
    let slow = Task { await controller.openBot("slow") }
    for _ in 0..<100 where !controller.openingBot { await Task.yield() }
    #expect(controller.openingBot)
    await controller.openBot("fast")
    await slow.value
    #expect(controller.bot?.id == "fast")
    await controller.openBot("missing")
    #expect(controller.bot == nil && !controller.openingBot)
    #expect(controller.openError == "Not found.")
    await controller.openBot("fast")
    #expect(controller.openError == nil && controller.bot?.id == "fast")
    await controller.openBot(nil)
    #expect(controller.bot == nil && controller.openError == nil)
  }

  @Test func runNavigationClearsStaleEvidenceAndRecoversFromAnError() async throws {
    let connection = try await service()
    defer { connection.shutDown() }
    let controller = ScheduleController(service: connection)
    await controller.openRun("fast")
    #expect(controller.run?.id == "fast")
    await controller.openRun("missing")
    #expect(controller.run == nil && controller.error == "Not found.")
    await controller.openRun("fast")
    #expect(controller.run?.id == "fast" && controller.error == nil)
  }
}
