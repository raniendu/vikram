import AppKit
import Foundation

/// The CLI's hidden browser: JSON lines in, `{"id", "result"}` lines out, until
/// input closes. The process never shows a window or a Dock icon, and its
/// private browsing data is gone when it exits.
@MainActor
public enum BrowserHelper {
    nonisolated static let maxLine = 1024 * 1024
    static let size = CGSize(width: 1280, height: 900)

    public static func run() -> Never {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        // WebKit lays out and paints pages only inside a window.
        let window = NSWindow(
            contentRect: CGRect(origin: .zero, size: size), styleMask: [.borderless],
            backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let engine = BrowserEngine(size: size)
        engine.onCreate = { view in
            view.frame = CGRect(origin: .zero, size: size)
            window.contentView = view
        }
        // The CLI waits for this line, so an app without helper mode is caught.
        write(#"{"ready":true}"#)
        let input = Thread {
            var buffer = Data()
            while true {
                let chunk = FileHandle.standardInput.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: 0x0A) {
                    let line = Data(buffer[buffer.startIndex..<newline])
                    buffer = Data(buffer[buffer.index(after: newline)...])
                    guard let message = try? JSONDecoder().decode(JSONValue.self, from: line)
                    else { continue }
                    // Queued in arrival order; the engine runs one action at a time.
                    DispatchQueue.main.async {
                        MainActor.assumeIsolated {
                            let action = engine.enqueue(message["call"] ?? .null)
                            Task { @MainActor in
                                let result = await action.value
                                let reply: JSONValue = ["id": message["id"] ?? .null, "result": result]
                                if let data = try? reply.encoded() {
                                    write(String(decoding: data, as: UTF8.self))
                                }
                            }
                        }
                    }
                }
                if buffer.count > maxLine { break }
            }
            // The CLI is done: the private session ends with this process.
            DispatchQueue.main.async { exit(0) }
        }
        input.start()
        app.run()
        exit(0)
    }

    nonisolated static func write(_ line: String) {
        FileHandle.standardOutput.write(Data((line + "\n").utf8))
    }
}
