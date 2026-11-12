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

  @ObservationIgnored private let service: ServiceConnection
  @ObservationIgnored private var openBotId: String?
  @ObservationIgnored private var pendingReload: Task<Void, Never>?

  public init(service: ServiceConnection) {
    self.service = service
  }

  private func fail(_ error: Error) {
    self.error =
      (error as? ServiceError)?.message ?? "This action could not finish. Try again."
  }

  public func refresh() async throws {
    do { try await reload() } catch {
      fail(error)
      throw error
    }
  }

  private func reload() async throws {
    home = try await service.call("home").decode(Home.self)
    let list = try await service.call("bots").decode(BotList.self)
    bots = list.bots
    background = list.background
    loaded = true
    if templates.isEmpty {
      templates =
        (try? await service.call("bot_templates").decode(TemplateList.self).templates)
        ?? []
    }
    if let id = openBotId {
      if list.bots.contains(where: { $0.id == id }) {
        bot = try await service.call("bot", ["task_id": .string(id)]).decode(Bot.self)
      } else {
        openBotId = nil
        bot = nil
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

  public func openBot(_ id: String?) async {
    openBotId = id
    guard let id else {
      bot = nil
      return
    }
    do {
      bot = try await service.call("bot", ["task_id": .string(id)]).decode(Bot.self)
    } catch {
      fail(error)
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
