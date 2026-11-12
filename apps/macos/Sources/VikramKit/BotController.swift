import Foundation
import Observation

/// Home and the bots: what needs the person, each bot's health, the open bot,
/// and the actions the window offers.
///
/// Bots run in the background like scheduled tasks; the service sends the same
/// change event for both, and this reloads Home and whatever bot is open.
@MainActor @Observable
public final class BotController {
  public private(set) var home = Home.empty
  public private(set) var bots: [Bot] = []
  public private(set) var templates: [BotTemplate] = []
  /// on, off, unavailable, or failed.
  public private(set) var background = "unavailable"
  public private(set) var bot: Bot?
  public var error: String?
  public private(set) var working = false
  public private(set) var loaded = false
  public private(set) var loading = false
  public private(set) var openingBot = false
  public private(set) var openError: String?
  public private(set) var templateError: String?

  @ObservationIgnored private let service: ServiceConnection
  @ObservationIgnored private var openBotId: String?
  @ObservationIgnored private var openGeneration = 0
  @ObservationIgnored private var reloadTask: Task<Void, Error>?
  @ObservationIgnored private var pendingReload: Task<Void, Never>?

  public init(service: ServiceConnection) {
    self.service = service
  }

  private func fail(_ error: Error) {
    self.error =
      (error as? ServiceError)?.message ?? "This action could not finish. Try again."
  }

  public func refresh() async throws {
    if let reloadTask { return try await reloadTask.value }
    let task = Task { try await self.reload() }
    reloadTask = task
    loading = true
    defer {
      loading = false
      reloadTask = nil
    }
    do {
      try await task.value
      error = nil
    } catch {
      fail(error)
      throw error
    }
  }

  private func reload() async throws {
    let home = try await service.call("home").decode(Home.self)
    let list = try await service.call("bots").decode(BotList.self)
    self.home = home
    bots = list.bots
    background = list.background
    loaded = true
    if templates.isEmpty || templateError != nil {
      do {
        templates = try await service.call("bot_templates").decode(TemplateList.self).templates
        templateError =
          templates.isEmpty ? "No templates were returned. Refresh or reconnect." : nil
      } catch {
        templateError =
          (error as? ServiceError)?.message ?? "Templates could not be loaded. Try again."
      }
    }
    if let id = openBotId {
      if list.bots.contains(where: { $0.id == id }) {
        await loadBot(id, generation: openGeneration)
      } else {
        openBotId = nil
        openGeneration += 1
        bot = nil
        openingBot = false
        openError = "This bot no longer exists. Go back to the list."
      }
    }
  }

  /// Supported templates open a fully seeded draft; blocked ones stay blocked.
  public func draft(templateId: String? = nil) -> BotDraft? {
    guard
      let template = templateId.flatMap({ id in templates.first { $0.id == id } })
        ?? (templateId == nil ? templates.first(where: \.available) : nil)
    else {
      error = "Templates have not loaded. Refresh the Bots page and try again."
      return nil
    }
    do { return try BotDraft(template: template) } catch {
      fail(error)
      return nil
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

  public func openBot(_ id: String?) async {
    openGeneration += 1
    openBotId = id
    bot = nil
    openError = nil
    openingBot = id != nil
    guard let id else { return }
    await loadBot(id, generation: openGeneration)
  }

  private func loadBot(_ id: String, generation: Int) async {
    do {
      let result = try await service.call("bot", ["task_id": .string(id)]).decode(Bot.self)
      guard openGeneration == generation, openBotId == id else { return }
      bot = result
      openingBot = false
      openError = nil
    } catch {
      guard openGeneration == generation, openBotId == id else { return }
      bot = nil
      openingBot = false
      openError = (error as? ServiceError)?.message ?? "This bot could not be opened. Try again."
    }
  }

  @discardableResult
  private func act(_ body: () async throws -> JSONValue) async -> JSONValue? {
    guard !working else { return nil }
    working = true
    error = nil
    defer { working = false }
    let result: JSONValue
    do { result = try await body() } catch {
      fail(error)
      return nil
    }
    do { try await refresh() } catch {
      // The mutation was acknowledged. Returning nil here made Create look
      // unsuccessful and invited duplicate bots when the person retried.
      self.error =
        "The action completed, but its updated status could not be loaded. Refresh to try again."
    }
    return result
  }

  /// Create a bot from its fields (a template fills the rest); the new bot, or nil.
  public func create(fields: JSONValue) async -> Bot? {
    guard let value = await act({ try await service.call("create_bot", ["fields": fields]) })
    else { return nil }
    return try? value.decode(Bot.self)
  }

  /// Change any of a bot's fields; true when saved.
  public func update(_ id: String, fields: JSONValue) async -> Bool {
    await act {
      try await service.call("update_bot", ["task_id": .string(id), "fields": fields])
    } != nil
  }

  public func setPaused(_ id: String, _ paused: Bool) async {
    await act {
      try await service.call(
        "update_bot", ["task_id": .string(id), "fields": ["enabled": .bool(!paused)]])
    }
  }

  public func runNow(_ id: String) async {
    await act { try await service.call("run_bot", ["task_id": .string(id)]) }
  }

  /// Convert one scheduled prompt into a paused bot, preserving its history.
  public func convertSchedule(_ id: String) async -> Bot? {
    guard
      let value = await act({
        try await service.call("convert_schedule", ["task_id": .string(id)])
      })
    else { return nil }
    return try? value.decode(Bot.self)
  }

  public func remove(_ id: String) async -> Bool {
    await act { try await service.call("delete_bot", ["task_id": .string(id)]) } != nil
  }

  /// Forget what the bot reported: open findings become resolved.
  public func reset(_ id: String) async {
    await act { try await service.call("reset_bot", ["task_id": .string(id)]) }
  }

  /// The bot's config as JSON, to save or share; it carries no cloud grant
  /// when imported again.
  public func export(_ id: String) async -> JSONValue? {
    await act { try await service.call("export_bot", ["task_id": .string(id)]) }
  }

  public func importBot(_ config: JSONValue) async -> Bot? {
    guard let value = await act({ try await service.call("import_bot", ["config": config]) })
    else { return nil }
    return try? value.decode(Bot.self)
  }

  /// seen, snoozed (until a date, or until the fact changes), resolved,
  /// dismissed, or new again.
  public func setFinding(_ id: String, state: String, until: Date? = nil) async {
    var params: [String: JSONValue] = ["finding_id": .string(id), "state": .string(state)]
    if let until {
      params["until"] = .number(until.timeIntervalSince1970)
    }
    await act { try await service.call("finding", params) }
  }

  /// Teach the bot: "material" or "not-material" (which also dismisses).
  public func feedback(_ id: String, verdict: String) async {
    await act {
      try await service.call(
        "finding_feedback", ["finding_id": .string(id), "verdict": .string(verdict)])
    }
  }

  /// Open findings that need a decision or an acknowledgement.
  public var attentionCount: Int { home.needsYou.count + home.stale.count }
}
