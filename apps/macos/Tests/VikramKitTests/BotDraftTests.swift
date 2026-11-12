import Foundation
import Testing

@testable import VikramKit

private func template(_ id: String = "page-watcher") throws -> BotTemplate {
  let value: JSONValue = [
    "id": .string(id), "title": "Page Watcher", "summary": "Watch pages.", "available": true,
    "defaults": [
      "mandate": "Tell me when these pages change.", "charter": "Ignore cosmetic changes.",
      "schedule": ["repeat": "daily", "time": "09:00"],
      "materiality": ["rubric": "Only substantive changes.", "keywords": ["visa"]],
      "expected_days": 3,
      // Even an accidentally supplied grant is not a grant for a new draft.
      "cloud": true, "model": "openrouter:example/model",
    ],
  ]
  return try value.decode(BotTemplate.self)
}

@Suite struct BotDraftTests {
  @Test func supportedTemplateSeedsTheWholeVisibleFormWithoutCloudPermission() throws {
    let draft = try BotDraft(template: template())
    #expect(draft.name == "Page Watcher")
    #expect(draft.mandate == "Tell me when these pages change.")
    #expect(draft.charter == "Ignore cosmetic changes.")
    #expect(draft.rubric == "Only substantive changes.")
    #expect(draft.keywords == "visa")
    #expect(draft.repeatKind == "daily")
    #expect(draft.expectedDays == 3)
    #expect(draft.fields["schedule"] == ["repeat": "daily", "time": "09:00"])
    #expect(draft.fields["template"] == "page-watcher")
    #expect(draft.fields["model"] == .null)
    #expect(!draft.cloud && !draft.originallyAllowedCloud)
    #expect(draft.validationMessage == nil)
  }

  @Test func changingTemplateChangesDefaultsButPreservesPersonalChoices() throws {
    var draft = try BotDraft(template: template())
    draft.name = "My monitor"
    draft.sources = "https://example.gov"
    draft.model = "local:1"
    draft.budget = 7
    var other = try template("release-watcher")
    other.defaults = [
      "mandate": "Watch releases.", "charter": "Breaking changes first.",
      "schedule": ["repeat": "weekly", "days": ["fri"], "time": "08:30"],
      "materiality": ["rubric": "Security changes are material."], "expected_days": 10,
    ]
    try draft.apply(other)
    #expect(draft.name == "My monitor")
    #expect(draft.sources == "https://example.gov")
    #expect(draft.model == "local:1" && draft.budget == 7)
    #expect(draft.mandate == "Watch releases.")
    #expect(draft.charter == "Breaking changes first.")
    #expect(draft.days == ["fri"])
    #expect(draft.fields["schedule"] == ["repeat": "weekly", "days": ["fri"], "time": "08:30"])
  }

  @Test func blockedAndMissingDefaultsCannotOpenACreationDraft() throws {
    var blocked = try template()
    blocked.available = false
    blocked.reason = "Repository sandbox required."
    #expect(throws: ServiceError.self) { try BotDraft(template: blocked) }
    var missing = try template()
    missing.defaults = nil
    #expect(throws: ServiceError.self) { try BotDraft(template: missing) }
  }

  @Test func invalidFormExplainsWhySaveIsDisabled() throws {
    var draft = try BotDraft(template: template())
    draft.name = "  "
    #expect(draft.validationMessage == "Enter a name for this bot.")
    draft.name = "Watch"
    draft.mandate = ""
    #expect(draft.validationMessage == "Describe the bot's responsibility.")
    draft.mandate = "Watch."
    draft.repeatKind = "weekly"
    draft.days = []
    #expect(draft.validationMessage == "Choose at least one day.")
    draft.days = ["mon"]
    draft.model = "openrouter:example/model"
    #expect(draft.validationMessage?.contains("explicitly") == true)
  }

  @Test func editingKeepsMonthlyScheduleAndDisabledLivenessWithoutGrantingAnything() throws {
    let value: JSONValue = [
      "id": "b1", "name": "Watcher", "mandate": "Watch.", "cloud": false,
      "expected_days": nil,
      "schedule": ["repeat": "monthly", "day_of_month": 31, "time": "17:45"],
      "config": [
        "charter": "My charter", "sources": ["https://example.gov"],
        "materiality": ["rubric": "My rubric", "examples": ["Keep this example"]],
      ],
    ]
    var draft = BotDraft(bot: try value.decode(Bot.self))
    draft.name = "Renamed"
    #expect(draft.botId == "b1")
    #expect(draft.fields["schedule"] == value["schedule"])
    #expect(draft.fields["expected_days"] == .null)
    #expect(draft.fields["charter"] == "My charter")
    #expect(draft.fields["template"] == nil)
    // Omitting unchanged advanced fields lets the backend preserve them.
    #expect(draft.fields["materiality"]?["examples"] == nil)
    #expect(throws: ServiceError.self) { try draft.apply(template()) }
  }
}
