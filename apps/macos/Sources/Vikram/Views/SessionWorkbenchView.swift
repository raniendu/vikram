import AppKit
import SwiftUI
import UniformTypeIdentifiers
import VikramKit

struct SessionWorkbenchView: View {
    @Environment(ChatController.self) private var chats
    @Environment(WorkbenchController.self) private var workbench
    @State private var instructions = ""
    @State private var selectedBranch: String?
    @State private var showTree = false

    private var busy: Bool { workbench.working || chats.active != nil || chats.sending }

    var body: some View {
        Form {
            Section("Conversation") {
                Text(chats.chat?.title ?? "Open a conversation first.").font(.headline)
                HStack {
                    Button("Fork Conversation") { Task { await workbench.perform("fork") } }
                    Button("Clone Conversation") { Task { await workbench.perform("clone") } }
                    Button("Show Branches") {
                        showTree = true
                        Task { await workbench.perform("tree") }
                    }
                }.disabled(busy || chats.chat == nil)
                Text("Forking and branching preserve the saved transcript. They do not undo file changes.")
                    .font(.caption).foregroundStyle(Theme.muted)
                if showTree {
                    branchList
                    HStack {
                        Button("Switch to Branch") {
                            guard let selectedBranch else { return }
                            Task { await workbench.perform("branch", value: selectedBranch) }
                        }
                        Button("Fork from Selection") {
                            guard let selectedBranch else { return }
                            Task { await workbench.perform("fork", value: selectedBranch) }
                        }
                    }.disabled(selectedBranch == nil || busy)
                }
            }
            Section("Context") {
                LabeledContent(
                    workbench.status?.context["estimated"]?.bool == false ? "Context used" : "Context used (estimated)",
                    value: contextCount(workbench.status?.context["tokens"] == nil ? "estimated_tokens" : "tokens"))
                LabeledContent("Model context limit", value: contextCount("context_limit"))
                if let usage = workbench.status?.context["last_usage"]?.object, !usage.isEmpty {
                    LabeledContent("Last reply input, all requests", value: "\(Int(usage["input_tokens"]?.number ?? 0).formatted()) tokens")
                    LabeledContent("Last reply output", value: "\(Int(usage["output_tokens"]?.number ?? 0).formatted()) tokens")
                    if let cost = usage["cost_usd"]?.string {
                        LabeledContent(usage["cost_estimated"]?.bool == true ? "Estimated cost (USD)" : "Reported cost (USD)", value: cost)
                    }
                }
                TextField("Optional instructions for the summary", text: $instructions)
                Button("Compact Context") {
                    Task { await workbench.perform("compact", value: instructions) }
                }.disabled(busy || chats.chat == nil)
                Text(
                    "Compaction preserves the full transcript and complete tool exchanges. Automatic compaction starts at 80% of a known context limit; unknown limits remain unknown."
                )
                .font(.caption).foregroundStyle(Theme.muted)
            }
            Section("Queued Input") {
                if workbench.status?.queue.isEmpty != false {
                    Text(
                        "While a reply is running, steer its next request or queue a follow-up from the composer. In other chats, Return queues the message to send after that reply."
                    )
                    .foregroundStyle(Theme.muted)
                }
                ForEach(workbench.status?.queue ?? []) { item in QueueEntryView(item: item) }
                QueueFootnote(runningElsewhere: nil)
            }
            Section("Local Files") {
                HStack {
                    Menu("Export…") {
                        Button("Markdown") { export("markdown") }
                        Button("HTML") { export("html") }
                        Button("JSON Lines") { export("jsonl") }
                    }.disabled(chats.chat == nil || workbench.working)
                    Button("Import JSON Lines…") { importConversation() }
                        .disabled(busy)
                }
            }
        }
        .formStyle(.grouped)
    }

    private var branchList: some View {
        List(selection: $selectedBranch) {
            ForEach(workbench.branches) { branch in
                HStack {
                    Image(systemName: branch.active == true ? "circle.inset.filled" : "circle")
                        .foregroundStyle(branch.active == true ? Theme.accent : Theme.muted)
                    Text(String(branch.text.prefix(140))).lineLimit(2)
                }
                .padding(.leading, CGFloat(depth(branch)) * 12)
                .tag(branch.id)
            }
        }
        .frame(height: 150)
        .accessibilityLabel("Conversation branches")
    }

    private func depth(_ branch: ConversationBranch) -> Int {
        var parent = branch.parentId
        var visited = Set([branch.id])
        var depth = 0
        while let id = parent, visited.insert(id).inserted, depth < 8 {
            depth += 1
            parent = workbench.branches.first { $0.id == id }?.parentId
        }
        return depth
    }

    private func contextCount(_ key: String) -> String {
        guard let count = workbench.status?.context[key]?.number else { return "Unknown" }
        return "\(Int(count).formatted()) tokens"
    }

    private func export(_ format: String) {
        Task {
            guard let result = await workbench.perform("export", options: ["format": .string(format)]),
                let content = result["content"]?.string
            else { return }
            let panel = NSSavePanel()
            let suffix = format == "markdown" ? "md" : format
            panel.nameFieldStringValue = "Conversation.\(suffix)"
            panel.allowedContentTypes = [UTType(filenameExtension: suffix) ?? .plainText]
            guard panel.runModal() == .OK, let url = panel.url else { return }
            do { try content.write(to: url, atomically: true, encoding: .utf8) } catch {
                workbench.error = "The conversation could not be exported. Choose another location."
            }
        }
    }

    private func importConversation() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.plainText, .json, .data]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            guard size <= 10 * 1024 * 1024 else {
                workbench.error = "Choose an export no larger than 10 MiB."
                return
            }
            let content = try String(contentsOf: url, encoding: .utf8)
            Task { await workbench.perform("import", value: content) }
        } catch { workbench.error = "Choose a readable UTF-8 JSON Lines export." }
    }
}

/// When queued messages start, and that they last only while Vikram runs.
struct QueueFootnote: View {
    /// The title of the chat whose reply runs now, when it is another one.
    let runningElsewhere: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if let runningElsewhere {
                Text("Sends after the reply in “\(runningElsewhere)” finishes. Queued messages start one at a time, oldest first.")
            }
            Text("Queued messages aren't saved: quitting Vikram discards them.")
        }
        .font(.caption)
        .foregroundStyle(Theme.muted)
    }
}

struct QueueEntryView: View {
    @Environment(WorkbenchController.self) private var workbench
    let item: QueuedInput
    @State private var editing = false
    @State private var text = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(item.kind.label).font(.caption.weight(.semibold)).foregroundStyle(Theme.accent)
                if item.isPaused {
                    Text("Paused").font(.caption).foregroundStyle(Theme.muted)
                        .help("Its reply stopped or failed, or it could not start. Resume to send it when no reply is running.")
                }
                Spacer()
                if item.isPaused {
                    Button("Resume") {
                        Task { await workbench.perform("queue_resume", value: item.id) }
                    }
                    .help("Send when no reply is running")
                }
                Button(editing ? "Save" : "Edit") {
                    if editing {
                        Task {
                            if await workbench.perform(
                                "queue_edit", value: item.id, options: ["text": .string(text)]) != nil
                            {
                                editing = false
                            }
                        }
                    } else {
                        text = item.text
                        editing = true
                    }
                }
                Button("Remove", systemImage: "xmark") {
                    Task { await workbench.perform("queue_remove", value: item.id) }
                }.labelStyle(.iconOnly)
            }
            .buttonStyle(.borderless)
            .disabled(workbench.working)
            if editing {
                TextField("Queued text", text: $text, axis: .vertical).lineLimit(2...5)
            } else {
                Text(item.text).font(.callout).lineLimit(3).textSelection(.enabled)
            }
        }
    }
}
