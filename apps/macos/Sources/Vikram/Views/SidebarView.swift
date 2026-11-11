import SwiftUI
import VikramKit

/// What a sidebar sheet edits.
enum SidebarSheet: Identifiable {
    case newFolder
    case renameFolder(Folder)
    case renameChat(ChatSummary)
    case moveChat(ChatSummary)

    var id: String {
        switch self {
        case .newFolder: "new-folder"
        case .renameFolder(let folder): "folder-\(folder.id)"
        case .renameChat(let chat): "rename-\(chat.id)"
        case .moveChat(let chat): "move-\(chat.id)"
        }
    }
}

/// The source list: scheduled tasks, unfiled chats, and folders of chats.
struct SidebarView: View {
    @Environment(AppState.self) private var state
    @Environment(ChatController.self) private var chats
    @Environment(ScheduleController.self) private var schedules
    @Environment(BotController.self) private var bots
    @Binding var sheet: SidebarSheet?
    @Binding var deletingChat: ChatSummary?
    @State private var search = ""
    @State private var collapsed: Set<String> = []
    @State private var deletingFolder: Folder?

    private var matching: [ChatSummary] {
        search.isEmpty
            ? chats.chats
            : chats.chats.filter { $0.title.localizedCaseInsensitiveContains(search) }
    }

    var body: some View {
        @Bindable var state = state
        List(selection: $state.selection) {
            Section {
                Label("Home", systemImage: "house")
                    .badge(bots.attentionCount)
                    .tag(SidebarItem.home)
                Label("Bots", systemImage: "sparkles")
                    .badge(bots.bots.count)
                    .tag(SidebarItem.bots)
                Label {
                    Text("Scheduled")
                } icon: {
                    Image(systemName: "calendar.badge.clock").foregroundStyle(Theme.accent)
                }
                .badge(schedules.tasks.count)
                    .tag(SidebarItem.scheduled)
            }
            if !search.isEmpty {
                Section {
                    ForEach(matching) { row($0) }
                    if matching.isEmpty {
                        Text("No matching chats.").foregroundStyle(Theme.muted)
                    }
                } header: {
                    Eyebrow("Results")
                }
            } else {
                Section {
                    ForEach(matching.filter { $0.folderId == nil }) { row($0) }
                    if chats.chats.isEmpty && !chats.loading {
                        Text("A little room for your thoughts.").foregroundStyle(Theme.muted)
                    }
                } header: {
                    Eyebrow("Default Bot · Conversations")
                }
                ForEach(chats.folders) { folder in
                    Section(
                        isExpanded: Binding(
                            get: { !collapsed.contains(folder.id) },
                            set: { open in
                                if open { collapsed.remove(folder.id) } else { collapsed.insert(folder.id) }
                            })
                    ) {
                        let inside = matching.filter { $0.folderId == folder.id }
                        ForEach(inside) { row($0) }
                        if inside.isEmpty {
                            Text("No chats yet").foregroundStyle(Theme.muted)
                        }
                    } header: {
                        Label {
                            Eyebrow(folder.name)
                        } icon: {
                            Image(systemName: "folder").foregroundStyle(Theme.muted)
                        }
                            .contextMenu {
                                Button("New Chat in Folder") { state.newChat(folder: folder.id) }
                                Button("Rename Folder…") { sheet = .renameFolder(folder) }
                                Divider()
                                Button("Delete Folder…", role: .destructive) { deletingFolder = folder }
                            }
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .scrollContentBackground(.hidden)
        .background(Theme.surface)
        .safeAreaInset(edge: .top, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                BrandMark(size: 22)
                Text("Vikram").font(Theme.display(17, weight: .medium))
                Eyebrow("Local")
                Spacer()
            }
            .padding(.horizontal, 18)
            .padding(.top, 4)
            .padding(.bottom, 6)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Vikram, local")
        }
        .searchable(text: $search, placement: .sidebar, prompt: "Search chats")
        .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 380)
        .safeAreaInset(edge: .bottom) {
            HStack {
                Button {
                    sheet = .newFolder
                } label: {
                    Label("New Folder", systemImage: "folder.badge.plus")
                }
                .buttonStyle(.borderless)
                .disabled(chats.offline)
                Spacer()
                Image(systemName: "lock.fill")
                    .foregroundStyle(Theme.muted)
                    .help("Conversations stay on this Mac")
                    .accessibilityLabel("Conversations stay on this Mac")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
        .toolbar {
            ToolbarItem {
                Button("New Chat", systemImage: "square.and.pencil") { state.newChat() }
                    .help("New chat (⌘N)")
                    .disabled(chats.offline || chats.loading)
            }
        }
        .confirmationDialog(
            "Delete the folder “\(deletingFolder?.name ?? "")”?",
            isPresented: Binding(get: { deletingFolder != nil }, set: { if !$0 { deletingFolder = nil } }),
            titleVisibility: .visible
        ) {
            Button("Delete Folder", role: .destructive) {
                if let folder = deletingFolder { Task { _ = await chats.deleteFolder(folder.id) } }
            }
        } message: {
            Text("Its chats will move to Chats.")
        }
    }

    private func row(_ chat: ChatSummary) -> some View {
        let running = chats.active?.chatId == chat.id
        return HStack(spacing: 6) {
            Text(chat.title).lineLimit(1)
            Spacer(minLength: 0)
            if running { ActivityDot().help("Reply in progress") }
        }
        .tag(SidebarItem.chat(chat.id))
        .contextMenu {
            Button("Rename…") { sheet = .renameChat(chat) }
            Menu("Move to") {
                Button("Chats") { move(chat, to: nil) }.disabled(chat.folderId == nil)
                if !chats.folders.isEmpty { Divider() }
                ForEach(chats.folders) { folder in
                    Button(folder.name) { move(chat, to: folder.id) }
                        .disabled(chat.folderId == folder.id)
                }
            }
            Divider()
            Button("Delete…", role: .destructive) { deletingChat = chat }
                .disabled(running || chats.offline || chats.sending || chats.importing)
        }
        .accessibilityLabel(running ? "\(chat.title), reply in progress" : chat.title)
    }

    private func move(_ chat: ChatSummary, to folder: String?) {
        Task {
            if chats.selected != chat.id { await chats.select(chat.id) }
            await chats.move(toFolder: folder)
        }
    }
}

/// A small sheet to name a folder or chat, or move a chat.
struct SidebarSheetView: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(ChatController.self) private var chats
    let sheet: SidebarSheet
    @State private var name = ""
    @State private var folder: String?
    @State private var working = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(title).font(Theme.display(17, weight: .medium, relativeTo: .headline))
            switch sheet {
            case .moveChat:
                Picker("Folder", selection: $folder) {
                    Text("Chats (no folder)").tag(String?.none)
                    ForEach(chats.folders) { Text($0.name).tag(Optional($0.id)) }
                }
            default:
                TextField(placeholder, text: $name)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(save)
            }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .buttonStyle(.secondary)
                    .keyboardShortcut(.cancelAction)
                Button(action: save) { Text(actionTitle) }
                    .buttonStyle(.primary)
                    .keyboardShortcut(.defaultAction)
                    .disabled(working || (needsName && name.trimmingCharacters(in: .whitespaces).isEmpty))
            }
        }
        .padding(20)
        .frame(width: 380)
        .tint(Theme.accent)
        .onAppear {
            switch sheet {
            case .renameFolder(let value): name = value.name
            case .renameChat(let chat): name = chat.title
            case .moveChat(let chat): folder = chat.folderId
            case .newFolder: break
            }
        }
    }

    private var needsName: Bool {
        if case .moveChat = sheet { return false }
        return true
    }

    private var title: String {
        switch sheet {
        case .newFolder: "New Folder"
        case .renameFolder: "Rename Folder"
        case .renameChat: "Rename Chat"
        case .moveChat: "Move Chat"
        }
    }

    private var placeholder: String {
        switch sheet {
        case .renameChat: "Chat title"
        default: "Ideas, Work, Learning"
        }
    }

    private var actionTitle: String {
        switch sheet {
        case .newFolder: "Create Folder"
        case .moveChat: "Move Chat"
        default: "Save"
        }
    }

    private func save() {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        guard !working, !needsName || !trimmed.isEmpty else { return }
        working = true
        Task {
            var done = true
            switch sheet {
            case .newFolder: done = await chats.createFolder(String(trimmed.prefix(80)))
            case .renameFolder(let value):
                done = await chats.renameFolder(value.id, to: String(trimmed.prefix(80)))
            case .renameChat(let chat):
                if chats.selected != chat.id { await chats.select(chat.id) }
                await chats.rename(String(trimmed.prefix(160)))
            case .moveChat(let chat):
                if chats.selected != chat.id { await chats.select(chat.id) }
                await chats.move(toFolder: folder)
            }
            working = false
            if done { dismiss() }
        }
    }
}
