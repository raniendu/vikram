import AppKit
import VikramKit

/// `vikram-desktop --browser-helper` runs the CLI's hidden browser; otherwise
/// this is the Vikram app.
@main
enum Entry {
    static func main() {
        if CommandLine.arguments.dropFirst().first == "--browser-helper" {
            MainActor.assumeIsolated { BrowserHelper.run() }
        }
        VikramApp.main()
    }
}
