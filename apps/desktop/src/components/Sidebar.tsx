import { useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Folder as FolderIcon,
  FolderPlus,
  MessageSquare,
  Plus,
  Search,
  Settings2,
  MoreHorizontal,
  Inbox,
  Trash2,
} from "lucide-react";
import type { ChatController } from "../useChats";
import type { ChatSummary, Folder } from "../types";

export function Sidebar({
  app,
  onSettings,
  onFolder,
  onClose,
  onDeleteChat,
}: {
  app: ChatController;
  onSettings: () => void;
  onFolder: (folder?: Folder) => void;
  onClose: () => void;
  onDeleteChat: (chat: ChatSummary) => void;
}) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<"all" | "unfiled">("all");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const matching = app.chats.filter((c) =>
    c.title.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
  );
  const rows = (chats: typeof matching) =>
    chats.map((chat) => (
      <div
        key={chat.id}
        className={`chat-row ${app.selected === chat.id ? "selected" : ""}`}
      >
        <button
          className="chat-open"
          onClick={() => {
            void app.select(chat.id);
            if (innerWidth < 900) onClose();
          }}
          title={chat.title}
          aria-current={app.selected === chat.id ? "page" : undefined}
        >
          <span
            className={
              app.active?.chat_id === chat.id ? "activity-dot" : "chat-dot"
            }
            aria-label={
              app.active?.chat_id === chat.id ? "Reply in progress" : undefined
            }
          />
          <span>{chat.title}</span>
        </button>
        <button
          className="icon-button chat-delete"
          aria-label={`Delete ${chat.title}`}
          title={
            app.active?.chat_id === chat.id
              ? "Stop the reply before deleting this conversation"
              : "Delete conversation"
          }
          disabled={
            app.offline ||
            app.loading ||
            app.sending ||
            app.importing ||
            app.active?.chat_id === chat.id
          }
          onClick={() => onDeleteChat(chat)}
        >
          <Trash2 size={14} />
        </button>
      </div>
    ));
  return (
    <aside className="sidebar" aria-label="Chat navigation">
      <div className="brand">
        <span className="brand-mark">v.</span>
        <span>Vikram</span>
        <span className="brand-note">LOCAL</span>
      </div>
      <button
        className="new-chat"
        onClick={() => void app.newChat()}
        disabled={app.offline || app.loading}
      >
        <Plus size={16} />
        New chat<span className="shortcut">⌘ N</span>
      </button>
      <div className="search">
        <Search size={15} />
        <input
          aria-label="Search chat titles"
          placeholder="Search chats"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <nav className="scope-nav" aria-label="Chat collections">
        <button
          className={filter === "all" ? "current" : ""}
          onClick={() => setFilter("all")}
        >
          <MessageSquare size={16} />
          All chats<span>{app.chats.length}</span>
        </button>
        <button
          className={filter === "unfiled" ? "current" : ""}
          onClick={() => setFilter("unfiled")}
        >
          <Inbox size={16} />
          Unfiled<span>{app.chats.filter((c) => !c.folder_id).length}</span>
        </button>
      </nav>
      <div className="sidebar-section">
        <span>
          {search
            ? "RESULTS"
            : filter === "unfiled"
              ? "UNFILED"
              : "CONVERSATIONS"}
        </span>
        <button
          className="icon-button"
          onClick={() => onFolder()}
          aria-label="New folder"
          disabled={app.offline}
        >
          <FolderPlus size={15} />
        </button>
      </div>
      <div className="chat-list">
        {rows(
          matching
            .filter((c) => search || !c.folder_id)
            .filter((c) => filter === "all" || !c.folder_id),
        )}
        {!search &&
          filter === "all" &&
          app.folders.map((folder) => (
            <div className="folder" key={folder.id}>
              <div className="folder-heading">
                <button
                  onClick={() =>
                    setCollapsed((old) => {
                      const next = new Set(old);
                      next.has(folder.id)
                        ? next.delete(folder.id)
                        : next.add(folder.id);
                      return next;
                    })
                  }
                  aria-expanded={!collapsed.has(folder.id)}
                >
                  {collapsed.has(folder.id) ? (
                    <ChevronRight size={13} />
                  ) : (
                    <ChevronDown size={13} />
                  )}
                  <FolderIcon size={15} />
                  <span>{folder.name}</span>
                  <span className="count">
                    {app.chats.filter((c) => c.folder_id === folder.id).length}
                  </span>
                </button>
                <button
                  className="icon-button folder-menu"
                  aria-label={`Manage ${folder.name}`}
                  onClick={() => onFolder(folder)}
                >
                  <MoreHorizontal size={15} />
                </button>
              </div>
              {!collapsed.has(folder.id) && (
                <div className="folder-chats">
                  {rows(matching.filter((c) => c.folder_id === folder.id))}
                  {!matching.some((c) => c.folder_id === folder.id) && (
                    <p className="empty-folder">No chats yet</p>
                  )}
                </div>
              )}
            </div>
          ))}
        {matching.length === 0 && (
          <p className="sidebar-empty">
            {search ? "No matching chats." : "A little room for your thoughts."}
          </p>
        )}
      </div>
      <div className="sidebar-footer">
        <button onClick={onSettings}>
          <Settings2 size={16} />
          Settings
        </button>
        <span className="local-dot" title="Conversations stay on this Mac" />
      </div>
    </aside>
  );
}
