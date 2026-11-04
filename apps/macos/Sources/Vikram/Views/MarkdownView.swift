import SwiftUI
import VikramKit

/// A Markdown reply as native text: paragraphs, headings, lists, quotes, code,
/// and tables. Links open in the default browser; images never load.
struct MarkdownView: View {
    let blocks: [MarkdownBlock]

    init(_ source: String) {
        blocks = Markdown.blocks(source)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(blocks) { block in
                BlockView(block: block)
            }
        }
        .environment(\.openURL, webLinks)
        .textSelection(.enabled)
    }
}

private struct BlockView: View {
    let block: MarkdownBlock

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            if block.listDepth > 0 {
                Text(block.marker ?? "")
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                    .frame(width: 22, alignment: .trailing)
                    .accessibilityHidden(block.marker == nil)
            }
            content
        }
        .padding(.leading, CGFloat(max(block.listDepth - 1, 0)) * 22)
        .padding(.leading, CGFloat(block.quoteDepth) * 14)
        .overlay(alignment: .leading) {
            if block.quoteDepth > 0 {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(.tertiary)
                    .frame(width: 3)
            }
        }
        .foregroundStyle(block.quoteDepth > 0 ? .secondary : .primary)
    }

    @ViewBuilder private var content: some View {
        switch block.kind {
        case .paragraph:
            Text(block.text).fixedSize(horizontal: false, vertical: true)
        case .heading(let level):
            Text(block.text)
                .font(level <= 1 ? .title2 : level == 2 ? .title3 : .headline)
                .bold()
                .padding(.top, 4)
                .accessibilityAddTraits(.isHeader)
        case .code(let language):
            CodeBox(title: language?.isEmpty == false ? language! : "code", text: block.plain)
        case .rule:
            Divider().padding(.vertical, 4)
        case .table(let table):
            TableView(table: table)
        }
    }
}

private struct TableView: View {
    let table: MarkdownTable

    var body: some View {
        ScrollView(.horizontal) {
            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                if !table.header.isEmpty {
                    GridRow {
                        ForEach(table.header.indices, id: \.self) { column in
                            cell(table.header[column], column).bold()
                        }
                    }
                    Divider()
                }
                ForEach(table.rows.indices, id: \.self) { row in
                    GridRow {
                        ForEach(table.rows[row].indices, id: \.self) { column in
                            cell(table.rows[row][column], column)
                        }
                    }
                }
            }
            .padding(10)
        }
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 8))
    }

    private func cell(_ text: AttributedString, _ column: Int) -> some View {
        let alignment: Alignment =
            switch table.alignments.indices.contains(column) ? table.alignments[column] : .leading {
            case .center: .center
            case .trailing: .trailing
            case .leading: .leading
            }
        return Text(text).gridColumnAlignment(alignment.horizontal)
    }
}
