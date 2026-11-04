import AppKit
import SwiftUI
import VikramKit
import WebKit

/// The browser Vikram drives, shown as an inspector beside the chat. The person
/// watches it, takes it over to use the page themselves, stops the reply, or
/// closes it, which ends its private session.
struct BrowserPanel: View {
    @Environment(AppState.self) private var state
    @Environment(ChatController.self) private var chats
    @Environment(BrowserEngine.self) private var browser

    private var address: String {
        guard let url = URL(string: browser.url), ["http", "https"].contains(url.scheme ?? "") else {
            return ""
        }
        let host = url.host(percentEncoded: false) ?? ""
        return url.path == "/" || url.path.isEmpty ? host : host + url.path
    }

    private var status: String {
        if chats.browserControl == .person {
            return "You’re in control. Vikram waits until you hand the browser back."
        }
        switch chats.phase {
        case "browsing": return "Vikram is using the browser…"
        case "approving": return "Vikram is waiting for your OK in the chat."
        default:
            return chats.active != nil
                ? "Vikram is replying."
                : "Private session · cleared when you close it or quit Vikram."
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: browser.url.hasPrefix("https:") ? "lock.fill" : "globe")
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 1) {
                    Text(browser.title.isEmpty ? (address.isEmpty ? "Browser" : address) : browser.title)
                        .font(.headline).lineLimit(1)
                    if !address.isEmpty && !browser.title.isEmpty {
                        Text(address).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                            .truncationMode(.middle)
                            .help(browser.url)
                    }
                }
                Spacer()
                if browser.loading { ProgressView().controlSize(.small) }
                Button {
                    state.closeBrowser()
                } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.borderless)
                .help("Close the browser and clear its private session")
                .accessibilityLabel("Close the browser")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            Divider()
            HStack(spacing: 8) {
                Text(status).font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if chats.browserControl == .person {
                    Button("Hand Back", systemImage: "arrow.uturn.backward") {
                        Task { await chats.controlBrowser(.agent) }
                    }
                    .buttonStyle(.borderedProminent)
                } else {
                    Button("Take Over", systemImage: "hand.raised") {
                        Task { await chats.controlBrowser(.person) }
                    }
                    .help("Use the page yourself; Vikram’s browser actions wait until you hand it back")
                }
                if chats.active != nil {
                    Button("Stop", systemImage: "stop.fill") { Task { await chats.stop() } }
                        .help("Stop the reply")
                }
            }
            .controlSize(.small)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(chats.browserControl == .person ? Color.accentColor.opacity(0.12) : .clear)
            .accessibilityElement(children: .contain)
            Divider()
            WebViewHost(webView: browser.webView)
                .overlay {
                    // While Vikram drives, a thin tint marks the page as busy.
                    if chats.active != nil && chats.browserControl == .agent {
                        RoundedRectangle(cornerRadius: 0)
                            .strokeBorder(Color.accentColor.opacity(0.5), lineWidth: 2)
                            .allowsHitTesting(false)
                    }
                }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Vikram’s browser")
    }
}

/// Shows the engine's page view; the view lives as long as the browser does.
private struct WebViewHost: NSViewRepresentable {
    let webView: WKWebView?

    func makeNSView(context: Context) -> NSView {
        NSView()
    }

    func updateNSView(_ container: NSView, context: Context) {
        guard let webView else {
            container.subviews.forEach { $0.removeFromSuperview() }
            return
        }
        if webView.superview !== container {
            container.subviews.forEach { $0.removeFromSuperview() }
            webView.removeFromSuperview()
            webView.frame = container.bounds
            webView.autoresizingMask = [.width, .height]
            container.addSubview(webView)
        }
    }
}
