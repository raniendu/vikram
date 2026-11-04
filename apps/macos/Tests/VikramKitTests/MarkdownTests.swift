import Foundation
import Testing

@testable import VikramKit

@Suite struct MarkdownTests {
    @Test func parsesBlocksWithTheirNesting() {
        let blocks = Markdown.blocks(
            """
            # Title

            Some *em* and `code`.

            1. One
               - nested
            2. Two

            > quoted

            ```python
            print(1)
            ```

            ---
            """)
        let kinds = blocks.map { block -> String in
            switch block.kind {
            case .paragraph: "p"
            case .heading(let level): "h\(level)"
            case .code(let language): "code:\(language ?? "")"
            case .rule: "hr"
            case .table: "table"
            }
        }
        #expect(kinds == ["h1", "p", "p", "p", "p", "p", "code:python", "hr"])
        #expect(blocks[2].marker == "1." && blocks[2].listDepth == 1)
        #expect(blocks[3].marker == "•" && blocks[3].listDepth == 2)
        #expect(blocks[4].marker == "2.")
        #expect(blocks[5].quoteDepth == 1)
        #expect(blocks[6].plain == "print(1)")
    }

    @Test func parsesTables() throws {
        let blocks = Markdown.blocks(
            """
            | Name | Count |
            |:-----|------:|
            | Tea  | 2     |
            | Milk | 10    |
            """)
        guard case .table(let table) = try #require(blocks.first).kind else {
            Issue.record("Expected a table")
            return
        }
        #expect(table.header.map { String($0.characters) } == ["Name", "Count"])
        #expect(table.rows.map { $0.map { String($0.characters) } } == [["Tea", "2"], ["Milk", "10"]])
        #expect(table.alignments == [.leading, .trailing])
    }

    @Test func neverLoadsImagesAndKeepsOnlyWebLinks() {
        let blocks = Markdown.blocks(
            "![chart](https://example.com/c.png) [site](https://example.com) [file](file:///etc/hosts)")
        let text = blocks[0].text
        #expect(String(text.characters).contains("[External image blocked: chart]"))
        let links = text.runs.compactMap(\.link)
        #expect(links == [URL(string: "https://example.com")!])
    }
}

