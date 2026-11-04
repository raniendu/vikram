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
        .tint(Theme.link)
        .textSelection(.enabled)
    }
}

private struct BlockView: View {
    let block: MarkdownBlock

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            if block.listDepth > 0 {
                Text(block.marker ?? "")
                    .foregroundStyle(Theme.muted)
                    .monospacedDigit()
                    .frame(width: 22, alignment: .trailing)
                    .accessibilityHidden(block.marker == nil)
            }
            content
        }
        .padding(.leading, CGFloat(max(block.listDepth - 1, 0)) * 22)
        .padding(.leading, CGFloat(block.quoteDepth) * 16)
        .overlay(alignment: .leading) {
            if block.quoteDepth > 0 {
                Rectangle()
                    .fill(Theme.accent.opacity(0.6))
                    .frame(width: 2)
            }
        }
        .foregroundStyle(block.quoteDepth > 0 ? Theme.muted : Theme.ink)
    }

    @ViewBuilder private var content: some View {
        switch block.kind {
        case .paragraph:
            Text(block.text).lineSpacing(3).fixedSize(horizontal: false, vertical: true)
        case .heading(let level):
            Text(block.text)
                .font(
                    Theme.display(
                        level <= 1 ? 26 : level == 2 ? 22 : 17, weight: .medium,
                        relativeTo: level <= 2 ? .title2 : .headline)
                )
                .padding(.top, 6)
                .accessibilityAddTraits(.isHeader)
        case .code(let language):
            CodeBox(title: language?.isEmpty == false ? language! : "code", text: block.plain)
        case .rule:
            Rectangle().fill(Theme.rule).frame(height: 1).padding(.vertical, 6)
        case .table(let table):
            TableView(table: table)
        }
    }
}

private struct TableView: View {
    let table: MarkdownTable

    var body: some View {
        ScrollView(.horizontal) {
            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 9) {
                if !table.header.isEmpty {
                    GridRow {
                        ForEach(table.header.indices, id: \.self) { column in
                            cell(table.header[column], column)
                                .font(.system(size: 10, weight: .semibold))
                                .tracking(1.2)
                                .textCase(.uppercase)
                        }
                    }
                    Rectangle().fill(Theme.ink).frame(height: 1).gridCellUnsizedAxes(.horizontal)
                }
                ForEach(table.rows.indices, id: \.self) { row in
                    GridRow {
                        ForEach(table.rows[row].indices, id: \.self) { column in
                            cell(table.rows[row][column], column)
                        }
                    }
                    Rectangle().fill(Theme.rule).frame(height: 1).gridCellUnsizedAxes(.horizontal)
                }
            }
            .padding(.vertical, 4)
        }
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
