import SwiftUI
import VikramKit

/// A small meter of how much of the model's context window the chat fills:
/// the accent below 80%, yellow from 80% (where compaction starts), red from 95%.
struct ContextUsageBar: View {
    var usage: ContextUsage

    private var tint: Color {
        switch usage.level {
        case .normal: Theme.accent
        case .warning: Theme.warning
        case .critical: Theme.danger
        }
    }

    private var detail: String {
        let counts = "\(usage.tokens.formatted()) of \(usage.limit.formatted()) tokens"
        return usage.estimated
            ? "About \(counts), estimated from the saved conversation. The next reply reports the model's own count."
            : "\(counts): the latest request's prompt and reply, as counted by the model."
    }

    var body: some View {
        HStack(spacing: 6) {
            Capsule()
                .fill(Theme.strongRule)
                .overlay(alignment: .leading) {
                    GeometryReader { proxy in
                        Capsule()
                            .fill(tint)
                            // Keep a sliver visible so a small share still reads as used.
                            .frame(width: max(proxy.size.width * usage.fraction, usage.tokens > 0 ? 4 : 0))
                    }
                }
                .frame(width: 72, height: 5)
            Text("\(usage.percentText) context")
                .monospacedDigit()
                .foregroundStyle(usage.level == .normal ? Theme.muted : tint)
        }
        .animation(.easeOut(duration: 0.25), value: usage)
        .help(detail)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Context used")
        .accessibilityValue(usage.percentText)
        .accessibilityHint(detail)
    }
}
