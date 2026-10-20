import AppKit

// This wrapper only presents installation progress. The Python runtime owns
// the same validation and activation transaction used by `vikram update`.
final class InstallerDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow?
    var installing = true

    func applicationDidFinishLaunching(_ notification: Notification) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 440, height: 150),
            styleMask: [.titled], backing: .buffered, defer: false
        )
        self.window = window
        window.title = "Install Vikram"
        window.center()
        let title = NSTextField(labelWithString: "Installing Vikram…")
        title.font = .systemFont(ofSize: 20, weight: .semibold)
        title.frame = NSRect(x: 28, y: 96, width: 384, height: 30)
        let detail = NSTextField(wrappingLabelWithString:
            "Setting up the Mac app and terminal command for your account.")
        detail.frame = NSRect(x: 28, y: 44, width: 335, height: 44)
        let spinner = NSProgressIndicator(frame: NSRect(x: 380, y: 52, width: 24, height: 24))
        spinner.style = .spinning
        spinner.startAnimation(nil)
        window.contentView?.addSubview(title)
        window.contentView?.addSubview(detail)
        window.contentView?.addSubview(spinner)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        guard let resources = Bundle.main.resourceURL else {
            finish(error: "The installer is incomplete. Download it again.")
            return
        }
        let payload = resources.appendingPathComponent("Vikram.app")
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = payload.appendingPathComponent("Contents/MacOS/vikram-sidecar")
            process.arguments = ["--install-bundle", payload.path]
            let output = Pipe()
            process.standardOutput = output
            process.standardError = output
            do {
                try process.run()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let result = try? JSONSerialization.jsonObject(with: data) as? [String: String]
                DispatchQueue.main.async {
                    if process.terminationStatus == 0, let result = result,
                       let version = result["version"], let app = result["app"],
                       let cli = result["cli"] {
                        self.finish(version: version, app: app, cli: cli)
                    } else {
                        self.finish(error: String(data: data.prefix(4000), encoding: .utf8)
                            ?? "Installation could not finish. Run the installer again.")
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self.finish(error: "The bundled installer could not start. Download a complete installer and try again.")
                }
            }
        }
    }

    func finish(version: String? = nil, app: String? = nil, cli: String? = nil, error: String? = nil) {
        installing = false
        window?.orderOut(nil)
        let alert = NSAlert()
        if let error = error {
            alert.alertStyle = .warning
            alert.messageText = "Vikram could not be installed"
            alert.informativeText = error
            alert.addButton(withTitle: "Close")
            alert.runModal()
        } else if let version = version, let app = app, let cli = cli {
            alert.messageText = "Vikram \(version) is installed"
            alert.informativeText = "Mac app: \(app)\n\nOpen a new terminal and run vikram. Update both with vikram update.\n\nCommand path: \(cli)\n\nOllama and a local model must already be installed. If Vikram is running, quit and reopen it to use this version."
            alert.addButton(withTitle: "Open Vikram")
            alert.addButton(withTitle: "Done")
            if alert.runModal() == .alertFirstButtonReturn {
                NSWorkspace.shared.open(URL(fileURLWithPath: app))
            }
        }
        NSApp.terminate(nil)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        return installing ? .terminateCancel : .terminateNow
    }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool { true }
}

let application = NSApplication.shared
let delegate = InstallerDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
