# Native macOS app

Date: November 3, 2026
Status: requested by the owner on November 3, 2026 ("convert this to a native
macOS app. Use best practices recommended by Apple."), implemented on the
`feat/native-macos-app` branch.

## 1. Outcome

Vikram.app becomes a native SwiftUI application. The Tauri shell (Rust), its
React/Vite web interface, and the npm and Cargo toolchains are removed. What the
person can do stays the same: saved chats and folders, per-chat models and
thinking, streamed replies with thinking, web activity and sources, attachments,
questions, plan mode, the Python sandbox, the agent browser, scheduled tasks,
updates, and the same privacy and safety rules.

The Python core does not change role. The app still starts the bundled
`vikram-sidecar` and speaks the same versioned JSON-lines protocol
(`vikram/desktop/protocol.py`), so the CLI and the app keep one agent, one
toolkit, and one context policy (AGENTS.md: "keep that behavior outside UI
adapters").

## 2. What stays fixed

- **Bundle layout.** `Vikram.app/Contents/MacOS/vikram-desktop` (the app's
  executable, `CFBundleExecutable`) and `Contents/MacOS/vikram-sidecar`, with
  bundle identifier `dev.raniendu.vikram`. The installer, `vikram update`, the
  scheduler's LaunchAgent, and installation checks depend on these names.
- **The browser contract.** `vikram-desktop --browser-helper` still runs the
  CLI's hidden browser: `{"ready":true}`, then `{"id","call"}` lines in and
  `{"id","result"}` lines out, until input closes. Results are `EngineResult`
  JSON, which `vikram/browser.py` validates.
- **The browser's safety.** WebKit with a non-persistent data store; the page
  script (`browser.js`) runs in its own `WKContentWorld`; a compiled content
  rule list blocks private and local network addresses for every request; the
  navigation policy allows only public `http`/`https` pages (plus `about:blank`,
  `about:srcdoc`, `blob:` and `data:` for frames); downloads and new windows are
  refused and reported; actions are validated before they reach the page; no
  developer tools. The panel never has a channel to the chat service: actions
  arrive from the service through the app and results go back the same way.
- **The chat library** in `~/Library/Application Support/dev.raniendu.vikram`,
  owned by the sidecar, and the 20 GiB free-space reserve.

## 3. Apple practices applied

- **SwiftUI app lifecycle** with one `Window` scene (one chat library, one
  service) and a `Settings` scene (⌘,). Menu commands for New Chat (⌘N), the
  sidebar (the standard View menu command), Check for Updates, and Stop Reply
  (⌘.).
- **`NavigationSplitView`** with a source-list sidebar (searchable, folders as
  disclosure groups, context menus for rename, move and delete) and a detail
  column; the agent browser is a trailing **inspector**. Toolbars hold the model
  and thinking pickers and chat actions. Confirmations use `confirmationDialog`
  and alerts; editing uses sheets.
- **Observation** (`@Observable`, `@MainActor`) for app state, and Swift
  structured concurrency for the service connection and the browser engine.
  The package builds in the Swift 6 language mode with complete concurrency
  checking.
- **The raniendu.dev theme** (the owner's choice on 2026-11-04, after comparing
  it with a GitHub-style theme): paper `#FBFAF8`, surface `#F4F2EC`, ink
  `#14130F`, rules `#E3E0D8`, and a rust accent `#A0552D` in light appearance;
  espresso `#14130F`, parchment text, and an amber accent `#D6995F` in dark.
  Space Grotesk, bundled with its SIL Open Font License, sets titles and the
  person's own words; the system font sets everything else, so Dynamic Type and
  accessibility settings keep working. Colors are dynamic `NSColor`s, so they
  follow the system appearance or the Settings choice (`NSApp.appearance`).
  SF Symbols supply the icons.
- **Accessibility.** Native controls carry labels, help tags, and keyboard
  focus; activity cards are grouped elements with clear labels; status text is
  read by VoiceOver.
- **Markdown** rendered natively from Foundation's `AttributedString` Markdown
  parser (block structure from `PresentationIntent`), with code blocks, lists,
  quotes, tables, and copy buttons. Only `http`/`https` links open, in the
  default browser; remote images are never loaded.
- **Code signing.** The build signs nested code first, then the bundle, with
  the Hardened Runtime on the app's own executable. The sidecar keeps its
  current signing (see section 5). No entitlements are needed by the app
  executable.
- **App Sandbox is not adopted.** The app starts a helper that boots
  virtual machines, shares its library and CLI launcher with the terminal, and
  installs a per-user LaunchAgent for scheduled tasks; none of these work
  inside the App Sandbox. This matches Apple's guidance for apps distributed
  outside the Mac App Store, which require Hardened Runtime and notarization
  (still pending Developer ID signing, docs/releases.md) but not the sandbox.

## 4. Structure

`apps/macos/` is a Swift package:

- `VikramKit` (library): protocol models, the service connection, the chat,
  schedule and update controllers, the browser engine and helper, the URL
  policy, model-name formatting, and Markdown block parsing.
- `Vikram` (executable `vikram-desktop`): the SwiftUI app and its views.
- `VikramKitTests`: offline tests for state recovery, the URL policy, action
  validation, Markdown parsing, model names, and the page script in a real
  `WKWebView`.
- `Resources/`: `Info.plist` (the app's version), `AppIcon.icns`, and
  `browser.js`.

`scripts/build_desktop.py` builds the sidecar, runs `swift build -c release`,
assembles `apps/macos/build/Vikram.app`, signs it, and runs the same startup
checks as before (sandbox VM, bundled browser on a blank page, desktop protocol
in an empty library).

## 5. Out of scope

Developer ID signing, notarization, Hardened Runtime for the PyInstaller
sidecar, and the App Sandbox. They need the owner's signing identity and a
decision about bundling Python differently, and are tracked in
docs/releases.md.
