import AppKit
import SwiftUI
import UniformTypeIdentifiers
import VikramKit

/// Where the person writes: the draft, its attachments, plan mode, and send or
/// stop. It floats over the end of the transcript.
struct ComposerView: View {
    @Environment(AppState.self) private var state
    @Environment(ChatController.self) private var chats
    @Environment(WorkbenchController.self) private var workbench
    let chat: Chat
    @State private var height: CGFloat = 22
    @State private var targeted = false

    private var pending: [Attachment] { chat.attachments.filter { $0.messageId == nil } }
    private var planning: Bool { chat.mode == .plan }
    private var runningHere: Bool { chats.active?.chatId == chat.id }
    private var busyElsewhere: Bool { chats.active != nil && !runningHere }
    private var invalidImages: Bool {
        pending.contains { !$0.isDocument } && chats.model?.vision != true
    }
    private var canQueue: Bool {
        runningHere && !chats.offline && !workbench.working
            && !chats.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let error = workbench.error, runningHere { notice(error) }
            if let queued = workbench.status?.queue, !queued.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(queued.prefix(3))) { item in QueueEntryView(item: item) }
                    if queued.count > 3 {
                        Button("View all \(queued.count) queued messages") { state.showsWorkbench = true }
                            .buttonStyle(.borderless)
                    }
                }
                .padding(10)
                .background(Theme.surface, in: RoundedRectangle(cornerRadius: 10))
            }
            if invalidImages {
                notice("Choose an image-capable model or remove the draft images to send.")
            }
            if planning {
                notice(
                    "Plan mode · Vikram proposes a plan for your approval before it runs code or does the work.",
                    systemImage: "list.bullet.clipboard")
            }
            if busyElsewhere {
                let title =
                    chats.chats.first { $0.id == chats.active?.chatId }?.title ?? "another chat"
                notice("A reply is running in “\(title)”. Your draft stays here.")
            }
            VStack(alignment: .leading, spacing: 10) {
                if !pending.isEmpty {
                    AttachmentStrip(
                        attachments: pending,
                        remove: chats.sending || chats.importing
                            ? nil : { item in Task { await chats.removeAttachment(item.id) } })
                }
                ZStack(alignment: .topLeading) {
                    if chats.draft.isEmpty {
                        Text(planning ? "Describe what you want planned…" : "Write a message…")
                            .foregroundStyle(Theme.muted)
                            .padding(.leading, 5)
                            .allowsHitTesting(false)
                            .accessibilityHidden(true)
                    }
                    ComposerTextView(
                        text: Binding(get: { chats.draft }, set: { chats.changeDraft($0) }),
                        height: $height,
                        focusKey: chat.id,
                        editable: !chats.sending,
                        submit: {
                            if canQueue { Task { await workbench.queueDraft(kind: .steer) } }
                            else if chats.canSend { Task { await chats.send() } }
                        },
                        followup: { if canQueue { Task { await workbench.queueDraft(kind: .followup) } } },
                        stop: runningHere ? { Task { await chats.stop() } } : nil,
                        importFiles: { urls in Task { await chats.importFiles(urls) } },
                        importImage: { data in
                            Task { await chats.importData(data, name: "Pasted image.png") }
                        }
                    )
                    .frame(height: min(max(height, 22), 200))
                }
                HStack(spacing: 8) {
                    Button {
                        state.pickAttachments()
                    } label: {
                        Image(systemName: "paperclip")
                            .font(.system(size: 15))
                            .frame(width: 30, height: 30)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Theme.muted)
                    .help("Attach images, PDF, Word, text or code, CSV, or Excel")
                    .accessibilityLabel("Attach files")
                    .disabled(chats.offline || chats.sending || chats.importing)
                    Button {
                        Task { await chats.setMode(planning ? .normal : .plan) }
                    } label: {
                        Label("Plan", systemImage: "list.bullet.clipboard")
                            .font(.caption.weight(.medium))
                            .padding(.horizontal, 11)
                            .frame(minHeight: 28)
                            .foregroundStyle(planning ? Theme.accent : Theme.muted)
                            .background(planning ? Theme.accent.opacity(0.12) : .clear, in: Capsule())
                            .overlay {
                                Capsule().strokeBorder(planning ? Theme.accent.opacity(0.5) : Theme.rule)
                            }
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(planning ? .isSelected : [])
                    .help(
                        planning
                            ? "Plan mode is on: approve a plan before Vikram works"
                            : "Plan mode: approve a plan before Vikram works")
                    .disabled(chats.offline)
                    Text(chats.importing ? "Importing files…" : runningHere ? "Return steers · Option + Return queues" : "Shift + Return for a new line")
                        .font(.caption).foregroundStyle(Theme.muted)
                    Spacer()
                    if runningHere {
                        Button("Steer") { Task { await workbench.queueDraft(kind: .steer) } }
                            .buttonStyle(.borderless).disabled(!canQueue)
                            .help("Send at the next request boundary after tools finish")
                        Button("Queue") { Task { await workbench.queueDraft(kind: .followup) } }
                            .buttonStyle(.borderless).disabled(!canQueue)
                            .help("Send as a follow-up when this reply finishes")
                        roundButton(systemImage: "stop.fill", fill: Theme.ink, foreground: Theme.paper) {
                            Task { await chats.stop() }
                        }
                        .help("Stop reply (⌘.)")
                        .accessibilityLabel("Stop reply")
                    } else if chats.sending {
                        ProgressView().controlSize(.small).frame(width: 34, height: 34)
                    } else {
                        roundButton(
                            systemImage: "arrow.up",
                            fill: chats.canSend ? Theme.accent : Theme.rule,
                            foreground: chats.canSend ? Theme.onAccent : Theme.muted
                        ) {
                            Task { await chats.send() }
                        }
                        .disabled(!chats.canSend)
                        .help(busyElsewhere ? "Wait for the active reply to finish" : "Send message")
                        .accessibilityLabel("Send message")
                    }
                }
            }
            .padding(.leading, 16)
            .padding([.trailing, .top, .bottom], 12)
            .background(Theme.raised, in: RoundedRectangle(cornerRadius: 18))
            .overlay {
                RoundedRectangle(cornerRadius: 18)
                    .strokeBorder(targeted ? Theme.accent : Theme.rule, lineWidth: targeted ? 2 : 1)
            }
            .shadow(color: .black.opacity(0.08), radius: 16, y: 8)
            .dropDestination(for: URL.self) { urls, _ in
                Task { await chats.importFiles(urls) }
                return true
            } isTargeted: { targeted = $0 }
            HStack {
                let tools = chats.model?.tools == true
                Text(
                    "\(chats.model?.providerLabel ?? "No model") · Web \(tools ? "available" : "unavailable")"
                        + (tools ? " · Python sandbox · Browser" : ""))
                Spacer()
                Text(
                    chats.model?.contextLength.map {
                        "\($0.formatted()) token context"
                    } ?? "Saved history · finite model context")
            }
            .font(.caption)
            .foregroundStyle(Theme.muted)
            .padding(.horizontal, 6)
        }
        .frame(maxWidth: 720)
        .padding(.horizontal, 28)
        .padding(.bottom, 14)
        .padding(.top, 28)
        .frame(maxWidth: .infinity)
        .background {
            LinearGradient(
                stops: [.init(color: Theme.paper.opacity(0), location: 0), .init(color: Theme.paper, location: 0.35)],
                startPoint: .top, endPoint: .bottom)
        }
        .task(id: runningHere) { await workbench.refresh(chatId: chat.id) }
    }

    private func roundButton(
        systemImage: String, fill: Color, foreground: Color, action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(foreground)
                .frame(width: 34, height: 34)
                .background(fill, in: Circle())
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
    }

    private func notice(_ text: String, systemImage: String = "exclamationmark.triangle")
        -> some View
    {
        Label {
            Text(text).foregroundStyle(Theme.muted)
        } icon: {
            Image(systemName: systemImage).foregroundStyle(Theme.accent)
        }
        .font(.callout)
        .padding(.horizontal, 6)
    }
}

/// The draft editor: an AppKit text view, so Return sends while Shift-Return,
/// input methods, and the standard editing commands behave as on any Mac.
struct ComposerTextView: NSViewRepresentable {
    @Binding var text: String
    @Binding var height: CGFloat
    let focusKey: String
    let editable: Bool
    let submit: () -> Void
    let followup: (() -> Void)?
    let stop: (() -> Void)?
    let importFiles: ([URL]) -> Void
    let importImage: (Data) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        let view = DraftTextView()
        view.delegate = context.coordinator
        view.drawsBackground = false
        view.isRichText = false
        view.importsGraphics = false
        view.allowsUndo = true
        view.isAutomaticQuoteSubstitutionEnabled = false
        view.isAutomaticDashSubstitutionEnabled = false
        view.font = .preferredFont(forTextStyle: .body)
        view.textContainerInset = .zero
        view.textContainer?.lineFragmentPadding = 5
        view.isVerticallyResizable = true
        view.isHorizontallyResizable = false
        view.autoresizingMask = [.width]
        view.textContainer?.widthTracksTextView = true
        view.setAccessibilityLabel("Message")
        view.registerForDraggedTypes([.fileURL])
        scroll.documentView = view
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let view = scroll.documentView as? DraftTextView else { return }
        view.submit = submit
        view.followup = followup
        view.importFiles = importFiles
        view.importImage = importImage
        view.isEditable = editable
        if view.string != text && !view.hasMarkedText() {
            view.string = text
            context.coordinator.measure(view)
        }
        if context.coordinator.focusKey != focusKey {
            context.coordinator.focusKey = focusKey
            DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        }
    }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ComposerTextView
        var focusKey: String?

        init(_ parent: ComposerTextView) { self.parent = parent }

        func textDidChange(_ notification: Notification) {
            guard let view = notification.object as? NSTextView else { return }
            parent.text = view.string
            measure(view)
        }

        func measure(_ view: NSTextView) {
            guard let container = view.textContainer, let layout = view.layoutManager else { return }
            layout.ensureLayout(for: container)
            let height = ceil(layout.usedRect(for: container).height)
            if abs(parent.height - height) > 0.5 {
                DispatchQueue.main.async { self.parent.height = height }
            }
        }

        func textView(_ view: NSTextView, doCommandBy selector: Selector) -> Bool {
            if selector == #selector(NSResponder.cancelOperation(_:)), let stop = parent.stop {
                stop()
                return true
            }
            guard selector == #selector(NSResponder.insertNewline(_:)) else { return false }
            let modifiers = NSApp.currentEvent?.modifierFlags ?? []
            if modifiers.contains(.shift) || modifiers.contains(.control) {
                view.insertNewlineIgnoringFieldEditor(nil)
            } else if modifiers.contains(.option), let followup = parent.followup {
                followup()
            } else {
                parent.submit()
            }
            return true
        }
    }
}

/// A text view that turns pasted or dropped files and images into attachments.
final class DraftTextView: NSTextView {
    var submit: (() -> Void)?
    var followup: (() -> Void)?
    var importFiles: (([URL]) -> Void)?
    var importImage: ((Data) -> Void)?

    override func keyDown(with event: NSEvent) {
        if (event.keyCode == 36 || event.keyCode == 76), event.modifierFlags.contains(.option),
            !event.modifierFlags.contains(.shift), !hasMarkedText(), let followup
        {
            followup()
            return
        }
        super.keyDown(with: event)
    }

    override func paste(_ sender: Any?) {
        let board = NSPasteboard.general
        if let urls = board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true])
            as? [URL], !urls.isEmpty
        {
            importFiles?(urls)
        } else if !board.canReadObject(forClasses: [NSString.self], options: nil),
            let image = NSImage(pasteboard: board), let tiff = image.tiffRepresentation,
            let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:])
        {
            importImage?(png)
        } else {
            pasteAsPlainText(sender)
        }
    }

    override func performDragOperation(_ sender: any NSDraggingInfo) -> Bool {
        if let urls = sender.draggingPasteboard.readObjects(
            forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL],
            !urls.isEmpty
        {
            importFiles?(urls)
            return true
        }
        return super.performDragOperation(sender)
    }
}
