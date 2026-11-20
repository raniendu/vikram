import AppKit
import SwiftTerm
import SwiftUI
import VikramKit

/// The command that runs the Vikram CLI: the bundled service's `--cli` mode,
/// or in a development build, the checkout's Python environment.
enum VikramCLI {
    static func command(_ arguments: [String]) -> (executable: String, arguments: [String])? {
        let bundled = Bundle.main.executableURL?.deletingLastPathComponent()
            .appendingPathComponent("vikram-sidecar")
        if let bundled, FileManager.default.isExecutableFile(atPath: bundled.path) {
            return (bundled.path, ["--cli"] + arguments)
        }
        #if DEBUG
            let repository = URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent().deletingLastPathComponent()
                .deletingLastPathComponent().deletingLastPathComponent()
                .deletingLastPathComponent().deletingLastPathComponent()
            let python = repository.appendingPathComponent(".venv/bin/python")
            if FileManager.default.isExecutableFile(atPath: python.path) {
                return (python.path, ["-m", "vikram"] + arguments)
            }
        #endif
        return nil
    }
}

/// A session's Terminal view: the real `vikram --session ID` in an embedded
/// terminal. The process ends when the view goes away, which releases the
/// session for the Chat view.
struct TerminalPane: NSViewRepresentable {
    let chatId: String
    let directory: String?
    let ended: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(ended: ended) }

    func makeNSView(context: Context) -> LocalProcessTerminalView {
        let view = LocalProcessTerminalView(frame: .zero)
        view.processDelegate = context.coordinator
        view.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        view.nativeBackgroundColor = NSColor(Theme.surface)
        view.nativeForegroundColor = NSColor(Theme.ink)
        if let command = VikramCLI.command(["--session", chatId]) {
            var environment = ProcessInfo.processInfo.environment
            environment["TERM"] = "xterm-256color"
            environment["COLORTERM"] = "truecolor"
            view.startProcess(
                executable: command.executable, args: command.arguments,
                environment: environment.map { "\($0.key)=\($0.value)" },
                currentDirectory: directory ?? NSHomeDirectory())
        } else {
            view.feed(text: "The Vikram CLI is not available in this build.\r\n")
        }
        DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        return view
    }

    func updateNSView(_ view: LocalProcessTerminalView, context: Context) {}

    static func dismantleNSView(_ view: LocalProcessTerminalView, coordinator: Coordinator) {
        coordinator.closed = true
        view.terminate()
    }

    final class Coordinator: NSObject, LocalProcessTerminalViewDelegate {
        let ended: () -> Void
        var closed = false

        init(ended: @escaping () -> Void) { self.ended = ended }

        func sizeChanged(source: LocalProcessTerminalView, newCols: Int, newRows: Int) {}
        func setTerminalTitle(source: LocalProcessTerminalView, title: String) {}
        func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
        func processTerminated(source: TerminalView, exitCode: Int32?) {
            if !closed { ended() }
        }
    }
}
