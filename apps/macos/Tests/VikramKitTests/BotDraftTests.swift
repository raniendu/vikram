import Foundation
import Testing

@testable import VikramKit

@Suite struct BotDraftTests {
  @Test func aNewBotIsAPromptToolsAModelAndASchedule() {
    var draft = BotDraft()
    #expect(draft.botId == nil && !draft.cloud && !draft.originallyAllowedCloud)
    #expect(draft.fields["tools"] == ["web_search", "web_fetch", "sandbox"])
    #expect(draft.fields["schedule"] == ["repeat": "daily", "time": "09:00"])
    #expect(draft.fields["model"] == .null)
    #expect(draft.fields["icon"] == .null)
    #expect(draft.fields["template"] == nil)
    draft.name = " Swift "
    draft.charter = "Watch Swift Evolution.\n"
    draft.icon = "orb-3"
    draft.tools = ["sandbox", "web_search"]
    #expect(draft.validationMessage == nil)
    #expect(draft.fields["name"] == "Swift")
    #expect(draft.fields["charter"] == "Watch Swift Evolution.")
    // The service derives the description from the prompt.
    #expect(draft.fields["mandate"] == "")
    #expect(draft.fields["icon"] == "orb-3")
    #expect(draft.fields["tools"] == ["web_search", "sandbox"])
    draft.tools = ["sandbox"]
    #expect(draft.validationMessage?.contains("web") == true)
  }

  @Test func invalidFormExplainsWhySaveIsDisabled() {
    var draft = BotDraft()
    draft.name = "  "
    #expect(draft.validationMessage == "Enter a name for this bot.")
    draft.name = "Watch"
    #expect(draft.validationMessage == "Write the bot's system prompt.")
    draft.charter = String(repeating: "x", count: 6001)
    #expect(draft.validationMessage?.contains("6,000") == true)
    draft.charter = "Watch."
    draft.repeatKind = "weekly"
    draft.days = []
    #expect(draft.validationMessage == "Choose at least one day.")
    draft.days = ["mon"]
    draft.model = "openrouter:example/model"
    #expect(draft.validationMessage?.contains("explicitly") == true)
  }

  @Test func editingKeepsScheduleToolsAndSavedSettingsWithoutGrantingAnything() throws {
    let value: JSONValue = [
      "id": "b1", "name": "Watcher", "mandate": "Tell me when pages change.", "cloud": false,
      "expected_days": nil, "icon": "orb-7", "tools": ["web_fetch"],
      "schedule": ["repeat": "monthly", "day_of_month": 31, "time": "17:45"],
      "config": [
        "charter": "Ignore cosmetic edits.", "sources": ["https://example.gov"],
        "materiality": ["rubric": "My rubric", "examples": ["Keep this example"]],
      ],
    ]
    var draft = BotDraft(bot: try value.decode(Bot.self))
    draft.name = "Renamed"
    #expect(draft.botId == "b1")
    #expect(draft.icon == "orb-7")
    #expect(draft.tools == ["web_fetch"])
    #expect(draft.fields["schedule"] == value["schedule"])
    #expect(draft.fields["expected_days"] == .null)
    // An older bot's responsibility leads its prompt, so saving loses nothing.
    #expect(draft.charter == "Tell me when pages change.\n\nIgnore cosmetic edits.")
    // Settings the editor does not show are left for the service to keep.
    #expect(draft.fields["sources"] == nil)
    #expect(draft.fields["materiality"] == nil)
    #expect(draft.fields["template"] == nil)
  }

  @Test func aPromptThatAlreadyStartsWithTheDescriptionIsNotRepeated() throws {
    let value: JSONValue = [
      "id": "b2", "name": "Swift", "mandate": "Watch Swift Evolution.", "cloud": false,
      "schedule": ["repeat": "daily", "time": "09:00"],
      "config": ["charter": "Watch Swift Evolution. Report accepted proposals."],
    ]
    let draft = BotDraft(bot: try value.decode(Bot.self))
    #expect(draft.charter == "Watch Swift Evolution. Report accepted proposals.")
    // A service without per-bot tools allows all of them.
    #expect(draft.tools == Set(BotDraft.allTools))
  }
}

@Suite struct AssistantTests {
  @Test func vikramDecodesWithItsPromptToolsAndModel() throws {
    let value: JSONValue = [
      "name": "Vikram", "icon": "orb-2", "instructions": "Be brief.",
      "tools": ["web_search", "browser"], "model": "local:qwen3:8b",
    ]
    let vikram = try value.decode(Assistant.self)
    #expect(vikram.id == "Vikram" && vikram.icon == "orb-2")
    #expect(vikram.instructions == "Be brief.")
    #expect(vikram.tools == ["web_search", "browser"])
    #expect(vikram.model == "local:qwen3:8b")
    #expect(Assistant.allTools.count == 5)
  }
}
