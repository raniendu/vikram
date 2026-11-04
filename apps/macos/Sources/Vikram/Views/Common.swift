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
        .foregroundStyle(.secondary)
        .help(label)
        .accessibilityLabel(copied ? "Copied" : label)
    }
}

/// A rounded card that holds a tool step waiting for the person.
struct CardStyle: ViewModifier {
    var highlighted = false

    func body(content: Content) -> some View {
        content
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.background.secondary, in: RoundedRectangle(cornerRadius: 10))
            .overlay {
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(highlighted ? Color.accentColor.opacity(0.6) : Color.secondary.opacity(0.2))
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
    var tint: Color = .orange
    @ViewBuilder var actions: Actions

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: systemImage).foregroundStyle(tint)
            Text(text).frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
            actions
        }
        .font(.callout)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(tint.opacity(0.12))
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
                Text(title).font(.caption.monospaced()).foregroundStyle(.secondary)
                Spacer()
                if let copyLabel { CopyButton(text: text, label: copyLabel) }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            Divider()
            ScrollView(.horizontal) {
                Text(text.isEmpty ? " " : text)
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: true, vertical: false)
                    .padding(10)
            }
        }
        .background(.background.secondary, in: RoundedRectangle(cornerRadius: 8))
        .overlay { RoundedRectangle(cornerRadius: 8).strokeBorder(.separator) }
    }
}
