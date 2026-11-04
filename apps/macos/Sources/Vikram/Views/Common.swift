import AppKit
import SwiftUI
import VikramKit

/// Open a web link in the default browser. Only `http` and `https` links without
/// credentials open; anything else is ignored.
@discardableResult
@MainActor func openWeb(_ url: URL) -> Bool {
    guard ["http", "https"].contains(url.scheme?.lowercased() ?? ""), url.user == nil,
        url.password == nil
    else { return false }
    return NSWorkspace.shared.open(url)
}

/// Links in replies and sources open only as web pages.
@MainActor let webLinks = OpenURLAction { url in
    openWeb(url) ? .handled : .discarded
}

func copyToPasteboard(_ text: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(text, forType: .string)
}

/// A page's host, for compact labels.
func host(_ text: String?) -> String {
    guard let text, let url = URL(string: text) else { return "" }
    return url.host(percentEncoded: false) ?? ""
}

/// A date the service sent as Unix seconds, in the local style.
func when(_ seconds: Double?) -> String {
    guard let seconds, seconds > 0 else { return "" }
    return Date(timeIntervalSince1970: seconds).formatted(
        .dateTime.weekday(.abbreviated).month(.abbreviated).day().hour().minute())
}

/// A button that copies text and briefly confirms it.
struct CopyButton: View {
    let text: String
    var label = "Copy"
    @State private var copied = false

    var body: some View {
        Button {
            copyToPasteboard(text)
            copied = true
            Task {
                try? await Task.sleep(for: .seconds(1.5))
                copied = false
            }
        } label: {
            Label(copied ? "Copied" : label, systemImage: copied ? "checkmark" : "doc.on.doc")
        }
        .buttonStyle(.borderless)
        .controlSize(.small)
        .foregroundStyle(Theme.muted)
        .help(label)
        .accessibilityLabel(copied ? "Copied" : label)
    }
}

/// A rounded card that holds a tool step waiting for the person.
struct CardStyle: ViewModifier {
    var highlighted = false

    func body(content: Content) -> some View {
        content
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                highlighted ? Theme.accent.opacity(0.1) : Theme.raised,
                in: RoundedRectangle(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .strokeBorder(highlighted ? Theme.accent.opacity(0.5) : Theme.rule)
            }
    }
}

extension View {
    func card(highlighted: Bool = false) -> some View {
        modifier(CardStyle(highlighted: highlighted))
    }
}

/// A one-line notice across the top of the chat, with optional actions.
struct Banner<Actions: View>: View {
    let text: String
    var systemImage = "exclamationmark.triangle.fill"
    var tint: Color = Theme.accent
    @ViewBuilder var actions: Actions

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: systemImage).foregroundStyle(tint)
            Text(text).frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
            actions
        }
        .font(.callout)
        .foregroundStyle(Theme.ink)
        .padding(.horizontal, 16)
        .padding(.vertical, 9)
        .background(tint.opacity(0.1))
        .overlay(alignment: .bottom) { Rectangle().fill(tint.opacity(0.3)).frame(height: 1) }
        .accessibilityElement(children: .contain)
    }
}

/// A small pulsing indicator for work in progress.
struct ActivityDot: View {
    var body: some View {
        ProgressView().controlSize(.mini)
            .accessibilityLabel("In progress")
    }
}

/// A large code or text block with a header and copy button.
struct CodeBox: View {
    let title: String
    let text: String
    var copyLabel: String? = "Copy code"

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(title).font(.caption.monospaced()).foregroundStyle(Theme.muted)
                Spacer()
                if let copyLabel { CopyButton(text: text, label: copyLabel) }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            Rectangle().fill(Theme.rule).frame(height: 1)
            ScrollView(.horizontal) {
                Text(text.isEmpty ? " " : text)
                    .font(.system(.callout, design: .monospaced))
                    .lineSpacing(3)
                    .foregroundStyle(Theme.ink)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: true, vertical: false)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
            }
        }
        .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10))
        .overlay { RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.rule) }
    }
}
