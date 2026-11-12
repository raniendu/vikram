import Foundation

/// Testable editor state shared by template selection, New Bot, and Edit.
/// Defaults come from the sidecar; creating a draft never grants cloud access.
public struct BotDraft: Identifiable, Sendable {
  public let id = UUID()
  public private(set) var botId: String?
  public private(set) var templateId: String?
  public var name = ""
  public var mandate = ""
  public var sources = ""
  public var charter = ""
  public var rubric = ""
  public var keywords = ""
  public var repeatKind = "weekly"
  public var days: Set<String> = ["mon"]
  public var clock: Date
  public var interval = 60
  public var model = ""
  public var cloud = false
  public var budget = 10
  public var expectedDays: Int? = 10
  public var notify = "attention"
  public let originallyAllowedCloud: Bool
  private var originalSchedule: ScheduleTime?

  public static let weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

  public init(template: BotTemplate) throws {
    clock = Self.time("08:00")
    originallyAllowedCloud = false
    try apply(template)
    name = template.title
  }

  public init(bot: Bot) {
    clock = Self.time(bot.schedule.time ?? "08:00")
    botId = bot.id
    templateId = bot.template
    name = bot.name
    mandate = bot.mandate
    model = bot.model ?? ""
    cloud = bot.cloud
    originallyAllowedCloud = bot.cloud
    budget = bot.budgetMinutes
    expectedDays = bot.expectedDays
    notify = bot.notify
    originalSchedule = bot.schedule
    loadSchedule(bot.schedule)
    if let config = bot.config {
      sources = Self.lines(config["sources"])
      charter = config["charter"]?.string ?? ""
      rubric = config["materiality"]?["rubric"]?.string ?? ""
      keywords = Self.lines(config["materiality"]?["keywords"])
    }
  }

  /// The template owns the responsibility, charter, rubric and schedule.
  /// Preserve the person's name, sources, model, and other explicit choices.
  public mutating func apply(_ template: BotTemplate) throws {
    guard botId == nil else {
      throw ServiceError("Change templates when creating a bot, not while editing one.")
    }
    guard template.available else {
      throw ServiceError(template.reason ?? "This template is not available yet.")
    }
    guard let defaults = template.defaults,
      let mandate = defaults["mandate"]?.string,
      let scheduleValue = defaults["schedule"],
      let schedule = try? scheduleValue.decode(ScheduleTime.self)
    else {
      throw ServiceError("Template defaults could not be loaded. Refresh or reconnect the service.")
    }
    templateId = template.id
    self.mandate = mandate
    charter = defaults["charter"]?.string ?? ""
    rubric = defaults["materiality"]?["rubric"]?.string ?? ""
    keywords = Self.lines(defaults["materiality"]?["keywords"])
    if let days = defaults["expected_days"]?.number, days.isFinite, (1...90).contains(days) {
      expectedDays = Int(days)
    } else {
      expectedDays = nil
    }
    loadSchedule(schedule)
  }

  private mutating func loadSchedule(_ schedule: ScheduleTime) {
    repeatKind = schedule.repeatKind
    days = Set(schedule.days ?? [])
    interval = schedule.everyMinutes ?? 60
    clock = Self.time(schedule.time ?? "08:00")
  }

  private static func time(_ value: String) -> Date {
    let parts = value.split(separator: ":").compactMap { Int($0) }
    let hour = parts.count == 2 ? parts[0] : 8
    let minute = parts.count == 2 ? parts[1] : 0
    return Calendar.current.date(from: DateComponents(hour: hour, minute: minute)) ?? .now
  }

  private static func lines(_ value: JSONValue?) -> String {
    value?.array?.compactMap(\.string).joined(separator: "\n") ?? ""
  }

  private func lines(_ text: String) -> [String] {
    text.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
      .filter { !$0.isEmpty }
  }

  /// Explain a disabled Save button rather than leaving the person guessing.
  public var validationMessage: String? {
    let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
    let mandate = mandate.trimmingCharacters(in: .whitespacesAndNewlines)
    if name.isEmpty { return "Enter a name for this bot." }
    if name.count > 80 { return "Use at most 80 characters for the name." }
    if mandate.isEmpty { return "Describe the bot's responsibility." }
    if mandate.count > 400 { return "Keep the responsibility within 400 characters." }
    if charter.count > 6000 { return "Keep standing instructions within 6,000 characters." }
    if rubric.count > 2000 { return "Keep the materiality rubric within 2,000 characters." }
    if lines(sources).count > 20 || lines(sources).contains(where: { $0.count > 500 }) {
      return "Use up to 20 sources, at most 500 characters each."
    }
    if lines(keywords).count > 20 || lines(keywords).contains(where: { $0.count > 300 }) {
      return "Use up to 20 keyword gates, at most 300 characters each."
    }
    if repeatKind == "weekly" && days.isEmpty { return "Choose at least one day." }
    if repeatKind == "interval" && !(15...10080).contains(interval) {
      return "Choose an interval from 15 minutes to one week."
    }
    if model.hasPrefix("openrouter:") && !cloud {
      return "Allow OpenRouter explicitly or choose a local model."
    }
    return nil
  }

  public var fields: JSONValue {
    let components = Calendar.current.dateComponents([.hour, .minute], from: clock)
    let time = String(format: "%02d:%02d", components.hour ?? 8, components.minute ?? 0)
    var schedule = ScheduleTime(
      repeatKind: repeatKind, time: time, days: Self.weekdays.filter(days.contains),
      everyMinutes: interval
    ).fields
    if !["daily", "weekly", "interval"].contains(repeatKind), let originalSchedule {
      schedule = originalSchedule.fields
    }
    var fields: [String: JSONValue] = [
      "name": .string(name.trimmingCharacters(in: .whitespacesAndNewlines)),
      "mandate": .string(mandate.trimmingCharacters(in: .whitespacesAndNewlines)),
      "sources": .array(lines(sources).map(JSONValue.string)),
      "charter": .string(charter), "schedule": schedule,
      "model": model.isEmpty ? .null : .string(model), "cloud": .bool(cloud),
      "budget_minutes": .number(Double(budget)),
      "expected_days": expectedDays.map { .number(Double($0)) } ?? .null,
      "notify": .string(notify),
      "materiality": [
        "rubric": .string(rubric),
        "keywords": .array(lines(keywords).map(JSONValue.string)),
      ],
    ]
    if botId == nil, let templateId { fields["template"] = .string(templateId) }
    return .object(fields)
  }
}
