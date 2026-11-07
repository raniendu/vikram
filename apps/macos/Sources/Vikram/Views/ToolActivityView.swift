import SwiftUI
import VikramKit

struct ToolActivityView: View {
    let record: ToolRecord
    let live: Bool
    let reply: (Reply) async -> Bool
    @State private var expanded = false
    @State private var sending = false

    var body: some View {
        if live {
            VStack(alignment: .leading, spacing: 10) {
                Label("Allow this \(record.kind) action?", systemImage: "exclamationmark.shield")
                    .font(Theme.display(16, weight: .medium, relativeTo: .headline))
                Text(record.title).fontWeight(.medium).textSelection(.enabled)
                if let reason = record.reason ?? record.approval?.reason {
                    Text(reason).font(.callout).foregroundStyle(Theme.muted)
                }
                details
                HStack {
                    Button("Don’t Allow") { send(.dismiss) }
                        .buttonStyle(.secondary)
                        .keyboardShortcut(.defaultAction)
                    Button("Allow Once") { send(.approve) }
                        .buttonStyle(.primary)
                }
                .disabled(sending)
            }
            .card(highlighted: true)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("\(record.kind) action for your approval")
        } else {
            ChipDisclosure(isExpanded: $expanded) {
                HStack(spacing: 5) {
                    if record.status == "running" { ActivityDot() }
                    Text("\(record.title) · \(record.status)")
                }
            } content: {
                VStack(alignment: .leading, spacing: 8) { details }
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    @ViewBuilder private var details: some View {
        if let parameters = record.approval?.details, !parameters.isEmpty {
            CodeBox(title: "parameters", text: parameters, copyLabel: nil)
        }
        if let target = record.url, !target.isEmpty {
            Text(target).font(.caption.monospaced()).textSelection(.enabled)
                .foregroundStyle(Theme.muted)
        }
        if let diff = record.diff, !diff.isEmpty { ToolDiffView(text: diff) }
        if let output = record.outputText, !output.isEmpty {
            CodeBox(title: "output", text: String(output.prefix(20000)), copyLabel: nil)
        }
        if let error = record.error {
            Text(error).font(.callout).foregroundStyle(Theme.danger).textSelection(.enabled)
        }
        ExtensionDeclarations(values: record.ui ?? [])
    }

    private func send(_ answer: Reply) {
        sending = true
        Task { if !(await reply(answer)) { sending = false } }
    }
}

private struct ToolDiffView: View {
    let text: String

    var body: some View {
        ScrollView([.horizontal, .vertical]) {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(
                    Array(String(text.prefix(30000)).components(separatedBy: "\n").enumerated()), id: \.offset
                ) { _, line in
                    Text(line.isEmpty ? " " : line)
                        .foregroundStyle(
                            line.hasPrefix("+")
                                ? Theme.success : line.hasPrefix("-") ? Theme.danger : Theme.ink)
                }
            }
            .font(.system(.caption, design: .monospaced))
            .textSelection(.enabled)
            .padding(10)
        }
        .frame(maxHeight: 220)
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 8))
        .accessibilityLabel("Proposed file changes")
    }
}
