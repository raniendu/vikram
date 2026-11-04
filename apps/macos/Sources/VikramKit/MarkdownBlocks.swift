import Foundation

/// One block of a Markdown reply, parsed with Foundation's Markdown support,
/// ready for native views: paragraphs, headings, code, rules, and tables, each
/// with its list and quote nesting.
public struct MarkdownBlock: Identifiable, Sendable {
    public enum Kind: Sendable {
        case paragraph
        case heading(level: Int)
        case code(language: String?)
        case rule
        case table(MarkdownTable)
    }

    public let id: Int
    public var kind: Kind
    /// Inline text with emphasis, code, and links (code blocks: plain text).
    public var text: AttributedString
    public var quoteDepth: Int
    public var listDepth: Int
    /// The list marker ("1.", "•") on the first block of a list item.
    public var marker: String?

    /// Plain text, for copying code blocks.
    public var plain: String { String(text.characters) }
}

public struct MarkdownTable: Sendable {
    public enum Alignment: Sendable { case leading, center, trailing }
    public var alignments: [Alignment]
    public var header: [AttributedString]
    public var rows: [[AttributedString]]
}

public enum Markdown {
    /// Parse `source` into blocks. Remote images are replaced by a notice and only
    /// web links survive, so rendering never loads anything.
    public static func blocks(_ source: String) -> [MarkdownBlock] {
        guard
            let parsed = try? AttributedString(
                markdown: source,
                options: .init(
                    allowsExtendedAttributes: false, interpretedSyntax: .full,
                    failurePolicy: .returnPartiallyParsedIfPossible))
        else {
            return [
                MarkdownBlock(
                    id: 0, kind: .paragraph, text: AttributedString(source), quoteDepth: 0,
                    listDepth: 0)
            ]
        }
        var blocks: [MarkdownBlock] = []
        var current: Int?
        var tables: [Int: Int] = [:]
        var rows: [Int: Int] = [:]
        var items = Set<Int>()
        for run in parsed.runs {
            let components = run.presentationIntent?.components ?? []
            var piece = AttributedString(parsed[run.range])
            if let image = run.imageURL {
                let alt = String(piece.characters)
                piece = AttributedString(
                    alt.isEmpty
                        ? "[External image blocked]" : "[External image blocked: \(alt)]")
                _ = image
            }
            if let link = run.link, !["http", "https"].contains(link.scheme?.lowercased() ?? "") {
                piece.link = nil
            }
            // A table cell joins its table's block.
            if let table = components.first(where: { if case .table = $0.kind { true } else { false } })
            {
                let cell = components.first {
                    if case .tableCell = $0.kind { true } else { false }
                }
                let row = components.first {
                    switch $0.kind {
                    case .tableRow, .tableHeaderRow: true
                    default: false
                    }
                }
                guard case .table(let columns) = table.kind, let cell, let row,
                    case .tableCell(let column) = cell.kind
                else { continue }
                let index: Int
                if let existing = tables[table.identity] {
                    index = existing
                } else {
                    index = blocks.count
                    tables[table.identity] = index
                    let alignments = columns.map { column -> MarkdownTable.Alignment in
                        switch column.alignment {
                        case .center: .center
                        case .right: .trailing
                        default: .leading
                        }
                    }
                    var block = block(id: index, components: components, items: &items)
                    block.kind = .table(
                        MarkdownTable(alignments: alignments, header: [], rows: []))
                    blocks.append(block)
                }
                guard case .table(var data) = blocks[index].kind else { continue }
                let width = data.alignments.count
                if case .tableHeaderRow = row.kind {
                    if data.header.isEmpty { data.header = Array(repeating: "", count: width) }
                    if column < width { data.header[column].append(piece) }
                } else {
                    if rows[row.identity] == nil {
                        rows[row.identity] = data.rows.count
                        data.rows.append(Array(repeating: "", count: width))
                    }
                    if let r = rows[row.identity], column < width { data.rows[r][column].append(piece) }
                }
                blocks[index].kind = .table(data)
                current = nil
                continue
            }
            let identity = components.first?.identity ?? -1
            if let current, blocks.indices.contains(current), blocks[current].id == identity {
                blocks[current].text.append(piece)
            } else {
                var block = block(id: identity, components: components, items: &items)
                block.text = piece
                blocks.append(block)
                current = blocks.count - 1
            }
        }
        // Code blocks end with a newline Markdown keeps; the view does not need it.
        for index in blocks.indices {
            if case .code = blocks[index].kind {
                var text = blocks[index].plain
                while text.hasSuffix("\n") { text.removeLast() }
                blocks[index].text = AttributedString(text)
            }
        }
        // Identities are unique per parse, but table blocks use their position.
        return blocks.enumerated().map { offset, block in
            var block = block
            block = MarkdownBlock(
                id: offset, kind: block.kind, text: block.text, quoteDepth: block.quoteDepth,
                listDepth: block.listDepth, marker: block.marker)
            return block
        }
    }

    private static func block(
        id: Int, components: [PresentationIntent.IntentType], items: inout Set<Int>
    ) -> MarkdownBlock {
        var kind = MarkdownBlock.Kind.paragraph
        switch components.first?.kind {
        case .header(let level)?: kind = .heading(level: level)
        case .codeBlock(let language)?: kind = .code(language: language)
        case .thematicBreak?: kind = .rule
        default: break
        }
        var quotes = 0
        var lists = 0
        var marker: String?
        for (offset, component) in components.enumerated() {
            switch component.kind {
            case .blockQuote: quotes += 1
            case .orderedList, .unorderedList: lists += 1
            case .listItem(let ordinal):
                // Only the innermost item's first block shows a marker.
                if marker == nil, lists == 0, !items.contains(component.identity) {
                    items.insert(component.identity)
                    let ordered: Bool
                    if offset + 1 < components.count,
                        case .orderedList = components[offset + 1].kind
                    {
                        ordered = true
                    } else {
                        ordered = false
                    }
                    marker = ordered ? "\(ordinal)." : "•"
                } else {
                    items.insert(component.identity)
                }
            default: break
            }
        }
        return MarkdownBlock(
            id: id, kind: kind, text: "", quoteDepth: quotes, listDepth: lists, marker: marker)
    }
}
