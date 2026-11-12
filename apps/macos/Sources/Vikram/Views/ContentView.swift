import SwiftUI
import VikramKit

/// The main window: the sidebar, a chat or the scheduled tasks, and the
/// browser as an inspector while it is open.
struct ContentView: View {
    @Environment(AppState.self) private var state
    @Environment(ChatController.self) private var chats
    @Environment(UpdateController.self) private var updates
    @Environment(WorkbenchController.self) private var workbench
    @State private var sheet: SidebarSheet?
    @State private var deletingChat: ChatSummary?

    var body: some View {
        NavigationSplitView {
            SidebarView(sheet: $sheet, deletingChat: $deletingChat)
        } detail: {
            Group {
                switch state.screen {
                case .home: HomeView()
                case .bots: BotsView()
                case .scheduled: SchedulesView()
                case .chat: ChatDetail(sheet: $sheet, deletingChat: $deletingChat)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Theme.paper)
            .safeAreaInset(edge: .top, spacing: 0) { banners }
        }
        .inspector(isPresented: state.browserShown) {
            BrowserPanel()
                .inspectorColumnWidth(min: 360, ideal: 560, max: 1000)
        }
        .tint(Theme.accent)
        .foregroundStyle(Theme.ink)
        .sheet(item: $sheet) { SidebarSheetView(sheet: $0) }
        .sheet(isPresented: Binding(get: { state.showsWorkbench }, set: { state.showsWorkbench = $0 })) {
            WorkbenchView()
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { state.showsWorkbench = true } label: {
                    Label(workbench.status?.workspace.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "Workbench",
                          systemImage: workbench.status?.trusted == true ? "folder.badge.checkmark" : "hammer")
                }
                .help("Workspace, providers, branches, queues, and integrations")
                .disabled(chats.offline)
            }
        }
        .task(id: chats.selected) {
            if !chats.offline { await workbench.refresh(chatId: chats.selected) }
        }
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
                Banner(text: error, tint: Theme.danger) {
                    Button("Reconnect") { Task { await state.reconnect() } }
                        .buttonStyle(.secondary)
                        .disabled(!state.canReconnect)
                    Button("Dismiss", systemImage: "xmark") { chats.error = nil }
                        .labelStyle(.iconOnly)
                        .buttonStyle(.borderless)
                }
            }
            if let version = updates.offer {
                Banner(
                    text: "Vikram \(version) is installed. Restart to use it.",
                    systemImage: "arrow.down.circle.fill"
                ) {
                    Button("Restart") { state.restart() }
                        .buttonStyle(.primary)
                    Button("Dismiss", systemImage: "xmark") { updates.dismiss() }
                        .labelStyle(.iconOnly)
                        .buttonStyle(.borderless)
                }
            }
            if let connection = chats.connection, !chats.offline {
                Banner(text: connection, systemImage: "bolt.horizontal.circle") {
                    Button("Refresh") { Task { await chats.refreshModels() } }
                        .buttonStyle(.secondary)
                        .disabled(chats.refreshing)
                }
            }
        }
    }
}

/// The open chat: its transcript under a floating composer, with the model
/// controls in the toolbar.
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
                Group {
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
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .safeAreaInset(edge: .top, spacing: 0) {
                    if let model = chats.model, !model.vision,
                        chat.attachments.contains(where: { $0.messageId != nil && !$0.isDocument })
                    {
                        Text(
                            "This model sees text only. Saved images stay in your chat and are omitted from its context."
                        )
                        .font(.callout)
                        .foregroundStyle(Theme.muted)
                        .padding(.vertical, 6)
                    }
                }
                .safeAreaInset(edge: .bottom, spacing: 0) { ComposerView(chat: chat) }
                .displayTitle(chat.title, subtitle: chats.model.map { "\($0.displayName) · \($0.providerLabel)" })
                .toolbar { ChatToolbar(chat: chat, sheet: $sheet, deletingChat: $deletingChat) }
            } else {
                Welcome(
                    title: "A thought starts here.",
                    text: "Your models. Your conversations. A quiet space, right on your Mac."
                ) {
                    Button("Start a Conversation") { state.newChat() }
                        .buttonStyle(.primary)
                        .disabled(chats.offline || chats.loading)
                }
            }
        }
    }
}

/// An empty view's greeting: the mark, a display headline, and a line of text.
struct Welcome<Actions: View>: View {
    let title: String
    let text: String
    @ViewBuilder var actions: Actions

    var body: some View {
        VStack(spacing: 14) {
            BrandMark(size: 44)
            Text(title)
                .font(Theme.display(34, weight: .medium, relativeTo: .largeTitle))
                .multilineTextAlignment(.center)
                .accessibilityAddTraits(.isHeader)
            Text(text)
                .font(.title3)
                .foregroundStyle(Theme.muted)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 460)
            actions.padding(.top, 6)
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct EmptyChat: View {
    @Environment(ChatController.self) private var chats

    var body: some View {
        Welcome(title: "A thought starts here.", text: description) {
            if !chats.models.contains(where: \.available) {
                Button("Refresh Models", systemImage: "arrow.clockwise") {
                    Task { await chats.refreshModels() }
                }
                .buttonStyle(.secondary)
                .disabled(chats.refreshing || chats.offline)
            } else if let model = chats.model {
                HStack(spacing: 6) {
                    Circle().fill(Theme.success).frame(width: 7, height: 7)
                    Text(
                        "\(model.displayName) · \(model.providerLabel) · \(model.vision ? "text & images" : "text")")
                }
                .font(.callout)
                .foregroundStyle(Theme.muted)
            }
        }
    }

    private var description: String {
        if chats.model != nil { return "Ask a question. Explore an idea. Make a little room for what comes next." }
        if !chats.models.contains(where: \.available) {
            return chats.connection != nil
                ? "Open Ollama to see your local models."
                : "Install a chat model in Ollama, or connect OpenRouter in Workbench, then refresh."
        }
        return "Choose a model in the toolbar to start a conversation."
    }
}

extension View {
    /// The window title in the display face where the system allows replacing
    /// it (macOS 15 and later); the standard title elsewhere.
    @ViewBuilder
    func displayTitle(_ title: String, subtitle: String?) -> some View {
        if #available(macOS 15.0, *) {
            self
                .navigationTitle(title)
                .toolbar(removing: .title)
                .toolbar {
                    ToolbarItem(placement: .navigation) {
                        VStack(alignment: .leading, spacing: 0) {
                            Text(title)
                                .font(Theme.display(17, weight: .medium, relativeTo: .headline))
                                .lineLimit(1)
                            if let subtitle {
                                Text(subtitle).font(.caption).foregroundStyle(Theme.muted)
                            }
                        }
                        .padding(.leading, 4)
                        .accessibilityElement(children: .combine)
                    }
                }
        } else {
            self.navigationTitle(title).navigationSubtitle(subtitle ?? "")
        }
    }
}

/// A toolbar menu labelled with a small caption and its value: MODEL Gemma 4.
/// One text, because toolbar menus keep only the first text of a label.
private struct LabeledMenuTitle: View {
    let caption: String
    let value: String

    var body: some View {
        Text(caption.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .tracking(1.3)
            .foregroundStyle(Theme.muted)
            + Text("  ") + Text(value).font(.callout)
    }
}

/// The open chat's model and thinking menus and its actions.
private struct ChatToolbar: ToolbarContent {
    @Environment(AppState.self) private var state
    @Environment(ChatController.self) private var chats
    let chat: Chat
    @Binding var sheet: SidebarSheet?
    @Binding var deletingChat: ChatSummary?

    private var model: LocalModel? { chats.model }

    var body: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            Menu {
                Picker(
                    "Model",
                    selection: Binding(
                        get: { chats.model?.name ?? chat.model ?? "" },
                        set: { name in Task { await state.chooseModel(name) } })
                ) {
                    if let current = chat.model, !chats.models.contains(where: { $0.matches(current) }) {
                        Text("\(formatModelName(current)) · unavailable").tag(current)
                    }
                    ForEach(chats.models) { model in
                        Text(
                            model.displayName + " · " + model.providerLabel + (model.vision ? " · images" : "")
                                + (model.available ? "" : " · unavailable")
                        )
                        .tag(model.name)
                        .selectionDisabled(!model.available)
                    }
                }
                .pickerStyle(.inline)
                .labelsHidden()
            } label: {
                LabeledMenuTitle(
                    caption: "Model",
                    value: model?.displayName ?? chat.model.map(formatModelName) ?? "Select a model")
            }
            .help(chat.model ?? "Choose a model")
            .accessibilityLabel("Model: \(chat.model.map(formatModelName) ?? "none")")
            .disabled(chats.offline || chats.loading || chats.sending)
            if let model, model.thinking.contains(where: { $0 != thinkingOn(model) }) {
                ThinkingMenu(chat: chat, model: model, on: thinkingOn(model))
            }
            Menu {
                Button("Rename…") { sheet = .renameChat(chat.summary) }
                Button("Move to Folder…") { sheet = .moveChat(chat.summary) }
                Button("Branches, Context, and Export…") { state.showsWorkbench = true }
                Divider()
                Button("Delete Chat…", role: .destructive) { deletingChat = chat.summary }
                    .disabled(
                        chats.active?.chatId == chat.id || chats.sending || chats.importing
                            || chats.offline || chats.loading)
            } label: {
                Label("Chat Actions", systemImage: "ellipsis")
            }
            .help("Chat actions")
        }
    }
}

private struct ThinkingMenu: View {
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

    private var onLabel: String {
        if case .level(let level)? = on { return "On · \(level)" }
        return "On"
    }

    var body: some View {
        Menu {
            Picker(
                "Thinking",
                selection: Binding(
                    get: { choice },
                    set: { value in Task { await chats.setThinking(value, for: model.name) } })
            ) {
                Text(onLabel).tag(Thinking?.none)
                ForEach(model.thinking.filter { $0 != on }, id: \.self) { value in
                    Text(value.label).tag(Optional(value))
                }
            }
            .pickerStyle(.inline)
            .labelsHidden()
        } label: {
            LabeledMenuTitle(caption: "Thinking", value: choice?.label ?? onLabel)
        }
        .help("How much the model thinks before answering")
        .accessibilityLabel("Thinking: \(choice?.label ?? onLabel)")
        .disabled(chats.sending)
    }
}
