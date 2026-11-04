import SwiftUI
import VikramKit

/// The main window: the sidebar, a chat or the scheduled tasks, and the
/// browser as an inspector while it is open.
struct ContentView: View {
    @Environment(AppState.self) private var state
    @Environment(ChatController.self) private var chats
    @Environment(UpdateController.self) private var updates
    @State private var sheet: SidebarSheet?
    @State private var deletingChat: ChatSummary?

    var body: some View {
        NavigationSplitView {
            SidebarView(sheet: $sheet, deletingChat: $deletingChat)
        } detail: {
            Group {
                if state.showsScheduled {
                    SchedulesView()
                } else {
                    ChatDetail(sheet: $sheet, deletingChat: $deletingChat)
                }
            }
            .safeAreaInset(edge: .top, spacing: 0) { banners }
        }
        .inspector(isPresented: state.browserShown) {
            BrowserPanel()
                .inspectorColumnWidth(min: 360, ideal: 560, max: 1000)
        }
        .sheet(item: $sheet) { SidebarSheetView(sheet: $0) }
        .confirmationDialog(
            "Delete “\(deletingChat?.title ?? "")”?",
            isPresented: Binding(get: { deletingChat != nil }, set: { if !$0 { deletingChat = nil } }),
            titleVisibility: .visible
        ) {
            Button("Delete Chat", role: .destructive) {
                if let chat = deletingChat { Task { await chats.deleteChat(chat.id) } }
            }
        } message: {
            Text(
                "Its messages and imported file copies will be permanently removed. Your original files stay where they are."
            )
        }
        .onChange(of: state.appearance, initial: true) { state.applyAppearance() }
    }

    @ViewBuilder private var banners: some View {
        VStack(spacing: 0) {
            if let error = chats.error {
                Banner(text: error) {
                    Button("Reconnect") { Task { await state.reconnect() } }
                        .disabled(!state.canReconnect)
                    Button("Dismiss", systemImage: "xmark") { chats.error = nil }
                        .labelStyle(.iconOnly)
                        .buttonStyle(.borderless)
                }
            }
            if let version = updates.offer {
                Banner(
                    text: "Vikram \(version) is installed. Restart to use it.",
                    systemImage: "arrow.down.circle.fill", tint: .accentColor
                ) {
                    Button("Restart") { state.restart() }
                    Button("Dismiss", systemImage: "xmark") { updates.dismiss() }
                        .labelStyle(.iconOnly)
                        .buttonStyle(.borderless)
                }
            }
            if let connection = chats.connection, !chats.offline {
                Banner(text: connection, systemImage: "bolt.horizontal.circle", tint: .orange) {
                    Button("Refresh") { Task { await chats.refreshModels() } }
                        .disabled(chats.refreshing)
                }
            }
        }
    }
}

/// The open chat: its transcript and composer, with the model controls in the toolbar.
private struct ChatDetail: View {
    @Environment(AppState.self) private var state
    @Environment(ChatController.self) private var chats
    @Binding var sheet: SidebarSheet?
    @Binding var deletingChat: ChatSummary?

    var body: some View {
        Group {
            if chats.loading && chats.chat == nil {
                ProgressView("Opening your conversations…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let chat = chats.chat {
                VStack(spacing: 0) {
                    if let model = chats.model, !model.vision,
                        chat.attachments.contains(where: { $0.messageId != nil && !$0.isDocument })
                    {
                        Text(
                            "This model sees text only. Saved images stay in your chat and are omitted from its context."
                        )
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 6)
                    }
                    if chat.messages.isEmpty {
                        EmptyChat()
                    } else {
                        TranscriptView(
                            chat: chat,
                            respond: { record, reply in
                                guard
                                    let attempt = chats.chat?.messages.last?.attempts.last,
                                    attempt.status == "running"
                                else { return false }
                                return await chats.respond(attempt: attempt, record: record, reply: reply)
                            },
                            handBack: { Task { await chats.controlBrowser(.agent) } },
                            openTask: { state.openTask($0) },
                            canRetry: state.canRetry,
                            retry: { Task { await chats.send(retry: true) } })
                    }
                    ComposerView(chat: chat)
                }
                .navigationTitle(chat.title)
                .navigationSubtitle(chats.model.map { formatModelName($0.name) } ?? "")
                .toolbar { ChatToolbar(chat: chat, sheet: $sheet, deletingChat: $deletingChat) }
            } else {
                ContentUnavailableView {
                    Label("A Thought Starts Here", systemImage: "bubble.left.and.text.bubble.right")
                } description: {
                    Text("Your models. Your conversations. A quiet space, right on your Mac.")
                } actions: {
                    Button("Start a Conversation") { state.newChat() }
                        .buttonStyle(.borderedProminent)
                        .disabled(chats.offline || chats.loading)
                }
            }
        }
    }
}

private struct EmptyChat: View {
    @Environment(ChatController.self) private var chats

    var body: some View {
        ContentUnavailableView {
            Label("A Thought Starts Here", systemImage: "sparkle")
        } description: {
            if let model = chats.model {
                Text(
                    "Ask a question. Explore an idea.\n\(formatModelName(model.name)) · \(model.vision ? "text & images" : "text")"
                )
            } else if !chats.models.contains(where: \.available) {
                Text(
                    chats.connection != nil
                        ? "Open Ollama to see your local models."
                        : "Install a chat model in Ollama, then refresh. Cloud models are unavailable here."
                )
            } else {
                Text("Choose a model in the toolbar to start a conversation.")
            }
        } actions: {
            if !chats.models.contains(where: \.available) {
                Button("Refresh Models", systemImage: "arrow.clockwise") {
                    Task { await chats.refreshModels() }
                }
                .disabled(chats.refreshing || chats.offline)
            }
        }
    }
}

/// The open chat's model and thinking pickers and its actions.
private struct ChatToolbar: ToolbarContent {
    @Environment(ChatController.self) private var chats
    let chat: Chat
    @Binding var sheet: SidebarSheet?
    @Binding var deletingChat: ChatSummary?

    private var model: LocalModel? { chats.model }

    var body: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            Picker(
                "Model",
                selection: Binding(
                    get: { chat.model ?? "" },
                    set: { name in Task { await chats.setModel(name) } })
            ) {
                if chat.model == nil { Text("Select a local model").tag("") }
                if let current = chat.model, !chats.models.contains(where: { $0.name == current }) {
                    Text("\(formatModelName(current)) · unavailable").tag(current)
                }
                ForEach(chats.models) { model in
                    Text(
                        formatModelName(model.name) + (model.vision ? " · images" : "")
                            + (model.available ? "" : " · unavailable")
                    )
                    .tag(model.name)
                    .selectionDisabled(!model.available)
                }
            }
            .help(chat.model ?? "Choose a local model")
            .disabled(chats.offline || chats.loading || chats.sending)
            if let model, model.thinking.contains(where: { $0 != thinkingOn(model) }) {
                ThinkingPicker(chat: chat, model: model, on: thinkingOn(model))
            }
            Menu {
                Button("Rename…") { sheet = .renameChat(chat.summary) }
                Button("Move to Folder…") { sheet = .moveChat(chat.summary) }
                Divider()
                Button("Delete Chat…", role: .destructive) { deletingChat = chat.summary }
                    .disabled(
                        chats.active?.chatId == chat.id || chats.sending || chats.importing
                            || chats.offline || chats.loading)
            } label: {
                Label("Chat Actions", systemImage: "ellipsis.circle")
            }
            .help("Chat actions")
        }
    }
}

private struct ThinkingPicker: View {
    @Environment(ChatController.self) private var chats
    let chat: Chat
    let model: LocalModel
    let on: Thinking?

    /// The saved choice, when the model still offers it; nil is the default ("on").
    private var choice: Thinking? {
        guard let saved = chat.summary.thinking[model.name] ?? nil, model.thinking.contains(saved),
            saved != on
        else { return nil }
        return saved
    }

    var body: some View {
        Picker(
            "Thinking",
            selection: Binding(
                get: { choice },
                set: { value in Task { await chats.setThinking(value, for: model.name) } })
        ) {
            if case .level(let level)? = on {
                Text("Thinking: On · \(level)").tag(Thinking?.none)
            } else {
                Text("Thinking: On").tag(Thinking?.none)
            }
            ForEach(model.thinking.filter { $0 != on }, id: \.self) { value in
                Text("Thinking: \(value.label)").tag(Optional(value))
            }
        }
        .help("How much the model thinks before answering")
        .disabled(chats.sending)
    }
}
