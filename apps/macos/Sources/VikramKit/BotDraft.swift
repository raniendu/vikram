import Foundation

/// Testable editor state shared by New Bot and Edit. A bot is a system prompt,
/// the tools it may use, a model, and a schedule. Creating a draft never grants
/// cloud access.
public struct BotDraft: Identifiable, Sendable {
  public let id = UUID()
  public private(set) var botId: String?
  public var name = ""
  public var icon: String?
  /// The bot's system prompt.
  public var charter = ""
  public var tools: Set<String> = Set(BotDraft.allTools)
  public var repeatKind = "daily"
  public var days: Set<String> = ["mon"]
  public var clock: Date
  public var interval = 60
  public var model = ""
  public var cloud = false
  public var budget = 10
  public var expectedDays: Int?
  public var notify = "attention"
  public let originallyAllowedCloud: Bool
  public private(set) var savedScheduleDescription: String?
  private var originalSchedule: ScheduleTime?

  public static let weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
  /// Everything an unattended run may use, in display order.
  public static let allTools = ["web_search", "web_fetch", "sandbox"]

  public init() {
    clock = Self.time("09:00")
    originallyAllowedCloud = false
  }

  public init(bot: Bot) {
    clock = Self.time(bot.schedule.time ?? "09:00")
    botId = bot.id
    name = bot.name
    icon = bot.icon
    model = bot.model ?? ""
    cloud = bot.cloud
    originallyAllowedCloud = bot.cloud
    budget = bot.budgetMinutes
    expectedDays = bot.expectedDays
    notify = bot.notify
    savedScheduleDescription = bot.description
    originalSchedule = bot.schedule
    if let tools = bot.tools { self.tools = Set(tools) }
    loadSchedule(bot.schedule)
    let saved = bot.config?["charter"]?.string ?? ""
    // Older bots kept their responsibility apart from their instructions;
    // the prompt keeps both, so saving loses nothing.
    let mandate = bot.mandate.trimmingCharacters(in: .whitespacesAndNewlines)
    charter =
      mandate.isEmpty || saved.hasPrefix(mandate)
      ? saved : saved.isEmpty ? mandate : "\(mandate)\n\n\(saved)"
  }

  private mutating func loadSchedule(_ schedule: ScheduleTime) {
    repeatKind = schedule.repeatKind
    days = Set(schedule.days ?? [])
    interval = schedule.everyMinutes ?? 60
    clock = Self.time(schedule.time ?? "09:00")
  }

  private static func time(_ value: String) -> Date {
    let parts = value.split(separator: ":").compactMap { Int($0) }
    let hour = parts.count == 2 ? parts[0] : 9
    let minute = parts.count == 2 ? parts[1] : 0
    return Calendar.current.date(from: DateComponents(hour: hour, minute: minute)) ?? .now
  }

  /// Explain a disabled Save button rather than leaving the person guessing.
  public var validationMessage: String? {
    let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
    let prompt = charter.trimmingCharacters(in: .whitespacesAndNewlines)
    if name.isEmpty { return "Enter a name for this bot." }
    if name.count > 80 { return "Use at most 80 characters for the name." }
    if prompt.isEmpty { return "Write the bot's system prompt." }
    if charter.count > 6000 { return "Keep the system prompt within 6,000 characters." }
    if tools.isDisjoint(with: ["web_search", "web_fetch"]) {
      return "Turn on web search or Read web pages; a bot checks the web before it reports."
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
    let time = String(format: "%02d:%02d", components.hour ?? 9, components.minute ?? 0)
    var schedule = ScheduleTime(
      repeatKind: repeatKind, time: time, days: Self.weekdays.filter(days.contains),
      everyMinutes: interval
    ).fields
    if !["daily", "weekly", "interval"].contains(repeatKind), let originalSchedule {
      schedule = originalSchedule.fields
    }
    let fields: [String: JSONValue] = [
      "name": .string(name.trimmingCharacters(in: .whitespacesAndNewlines)),
      // The service describes the bot by its prompt's first sentence.
      "mandate": .string(""),
      "charter": .string(charter.trimmingCharacters(in: .whitespacesAndNewlines)),
      "tools": .array(Self.allTools.filter(tools.contains).map(JSONValue.string)),
      "icon": icon.map(JSONValue.string) ?? .null,
      "schedule": schedule,
      "model": model.isEmpty ? .null : .string(model), "cloud": .bool(cloud),
      "budget_minutes": .number(Double(budget)),
      "expected_days": expectedDays.map { .number(Double($0)) } ?? .null,
      "notify": .string(notify),
    ]
    // Sources, rubric and keyword gates are not edited here; leaving them out
    // lets the service keep what an existing bot saved.
    return .object(fields)
  }
}
