import Foundation
import Testing

@testable import VikramKit

@Suite struct BrowserPolicyTests {
    @Test(arguments: [
        "https://example.com/",
        "http://news.example.org/a?b=c",
        "https://8.8.8.8/",
        "https://[2606:4700::1111]/",
        "about:blank",
        "about:srcdoc",
        "blob:https://example.com/0a1b",
        "data:text/html,hi",
    ])
    func allowsPublicWebPages(_ text: String) throws {
        #expect(BrowserPolicy.allowed(try #require(URL(string: text))))
    }

    @Test(arguments: [
        "http://localhost:11434/api/tags",
        "http://LOCALHOST./",
        "http://router/",
        "http://printer.local/",
        "http://app.localhost:1420/",
        "http://nas.home.arpa/",
        "http://127.0.0.1:8080/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://0177.0.0.1/",
        "http://10.0.0.1/",
        "http://172.16.5.4/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.101.102.103/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:192.168.1.1]/",
        "https://user:pass@example.com/",
        "file:///etc/passwd",
        "vikram://localhost/",
        "mailto:a@example.com",
        "javascript:alert(1)",
        "about:config",
    ])
    func blocksEverythingElse(_ text: String) throws {
        #expect(!BrowserPolicy.allowed(try #require(URL(string: text))))
    }

    @Test func contentRulesCoverPrivateHostsWithoutAlternation() throws {
        let rules = try JSONDecoder().decode(
            JSONValue.self, from: Data(BrowserPolicy.privateRules.utf8))
        let list = try #require(rules.array)
        #expect(list.count == BrowserPolicy.privateFilters.count)
        for rule in list {
            #expect(rule["action"]?["type"] == "block")
            let filter = try #require(rule["trigger"]?["url-filter"]?.string)
            // WebKit content blockers reject disjunctions.
            #expect(!filter.contains("|"))
            #expect(filter.hasPrefix("^[a-z]+://"))
        }
    }

    @Test func actionsAreValidatedBeforeTheyReachThePage() throws {
        let open = try BrowserPolicy.parse([
            "action": "open", "url": "https://example.com/", "screenshot": true,
        ])
        #expect(open.0 == .open(URL(string: "https://example.com/")!))
        #expect(open.screenshot)
        #expect(
            try BrowserPolicy.parse([
                "id": "b1", "action": "type", "element": 3, "text": "kettle", "submit": true,
            ]).0 == .type(element: 3, text: "kettle", submit: true, approved: false))
        #expect(try BrowserPolicy.parse(["action": "scroll", "direction": "up"]).0 == .scroll(up: true))
        let bad: [JSONValue] = [
            nil,
            ["action": "eval", "script": "alert(1)"],
            ["action": "click"],
            ["action": "click", "element": 0],
            ["action": "click", "element": "3"],
            ["action": "click", "element": 3, "approved": "yes"],
            ["action": "type", "element": 3, "text": .string(String(repeating: "x", count: 2001))],
            ["action": "scroll", "direction": "left"],
            ["action": "open", "url": "http://192.168.1.1/"],
            ["action": "open", "url": "about:blank"],
            ["action": "open", "url": "file:///etc/hosts"],
        ]
        for call in bad {
            #expect(throws: ServiceError.self) { try BrowserPolicy.parse(call) }
        }
    }
}
