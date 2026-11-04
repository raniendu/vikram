import Foundation
import Testing

@testable import VikramKit

/// The page script in a real WebKit view, offline: what it lists, refuses, and
/// asks approval for, and how the engine reports it.
@MainActor @Suite(.serialized) struct PageScriptTests {
    let base = URL(string: "https://shop.example/")!

    private func engine(_ html: String) async throws -> BrowserEngine {
        let engine = BrowserEngine(size: CGSize(width: 1024, height: 900))
        try await engine.load(html: "<title>Shop</title>" + html, base: base)
        return engine
    }

    private func look(_ engine: BrowserEngine) async throws -> [String: JSONValue] {
        let result = await engine.act(["action": "look"])
        #expect(result["ok"] == true, "\(result)")
        return try #require(result["page"]?.object)
    }

    private func ref(_ page: [String: JSONValue], _ name: String) throws -> Int {
        let element = try #require(
            page["elements"]?.array?.first { $0["name"]?.string == name }, "no \(name) in \(page)")
        return Int(try #require(element["ref"]?.number))
    }

    @Test func numbersWhatCanBeUsedAndHidesSensitiveValues() async throws {
        let engine = try await engine(
            """
            <label>Search <input id="q" value="copper"></label>
            <button>Go</button>
            <label>Password <input type="password" value="hunter2"></label>
            <label>Size <select><option>Small</option><option selected>Large</option></select></label>
            <p>Free shipping <b>today</b> only.</p>
            <script>window.ignored = 1</script>
            """)
        let page = try await look(engine)
        #expect(page["title"] == "Shop")
        let elements = try #require(page["elements"]?.array)
        #expect(elements.map { $0["name"]?.string ?? "" } == ["Search", "Go", "Password", "Size"])
        #expect(elements[0]["value"] == "copper")
        #expect(elements[2]["value"] == "(hidden)")
        #expect(elements[3]["value"] == "Large")
        let text = try #require(page["text"]?.string)
        #expect(text.contains("Free shipping today only."))
        #expect(!text.contains("hunter2"))
        engine.close()
    }

    @Test func asksBeforeBuyingAndClicksOnlyWhenApproved() async throws {
        let engine = try await engine(
            """
            <button type="button" onclick="document.title = 'Bought'">Buy now</button>
            <a href="/docs">Docs</a>
            """)
        let page = try await look(engine)
        let buy = try ref(page, "Buy now")
        let asked = await engine.act(["action": "click", "element": .number(Double(buy))])
        #expect(asked["ok"] == false)
        #expect(asked["approval"]?["reason"]?.string?.contains("buys or pays") == true)
        #expect(asked["approval"]?["summary"] == "Click “Buy now” on shop.example")
        #expect(try await look(engine)["title"] == "Shop")
        let done = await engine.act([
            "action": "click", "element": .number(Double(buy)), "approved": true,
        ])
        #expect(done["ok"] == true)
        #expect(done["page"]?["title"] == "Bought")
        engine.close()
    }

    @Test func neverTypesPasswordsAndFillsOtherFields() async throws {
        let engine = try await engine(
            """
            <label>Password <input type="password"></label>
            <label>Name <input id="name"></label>
            """)
        let page = try await look(engine)
        let refused = await engine.act([
            "action": "type", "element": .number(Double(try ref(page, "Password"))),
            "text": "secret", "approved": true,
        ])
        #expect(refused["ok"] == false)
        #expect(refused["refused"]?.string?.contains("never types") == true)
        // Typed text reaches the page only as data, never as code.
        let tricky = "\"); document.title = 'owned'; (\"\u{2028}"
        let typed = await engine.act([
            "action": "type", "element": .number(Double(try ref(page, "Name"))),
            "text": .string(tricky),
        ])
        #expect(typed["ok"] == true, "\(typed)")
        #expect(typed["page"]?["title"] == "Shop")
        let name = try #require(
            typed["page"]?["elements"]?.array?.first { $0["name"] == "Name" })
        #expect(name["value"]?.string?.contains("document.title = 'owned'") == true)
        engine.close()
    }

    @Test func refusesBlockedAddressesAndBadActions() async throws {
        let engine = BrowserEngine()
        let blocked = await engine.act(["action": "open", "url": "http://192.168.1.1/"])
        #expect(blocked["ok"] == false)
        #expect(blocked["error"]?.string?.contains("blocked") == true)
        #expect(!engine.isOpen)
        let invalid = await engine.act(["action": "eval", "script": "1"])
        #expect(invalid["error"] == "The browser received an invalid action.")
    }
}
