import { useEffect, useRef, useState } from "react";
import {
  FolderInput,
  Menu,
  MoreHorizontal,
  Pencil,
  RefreshCw,
  Trash2,
  X,
  ArrowUpRight,
  PanelLeft,
} from "lucide-react";
import { useChats } from "./useChats";
import { Sidebar } from "./components/Sidebar";
import { Dialog } from "./components/Dialog";
import { Transcript } from "./components/Transcript";
import { Composer } from "./components/Composer";
import { BrowserPanel } from "./components/BrowserPanel";
import { Schedules } from "./components/Schedules";
import { useSchedules } from "./useSchedules";
import { useUpdates } from "./useUpdates";
import { useBrowser } from "./useBrowser";
import { desktop, native, onNative } from "./bridge";
import { formatModelName } from "./modelNames";
import { thinkingOn } from "./state";
import type { ChatSummary, Folder, Thinking, UpdateStatus } from "./types";
import { version } from "../package.json";

export default function App() {
  const app = useChats();
  const browser = useBrowser();
  const schedules = useSchedules();
  const updates = useUpdates();
  // The main pane shows a chat, or the scheduled tasks.
  const [view, setView] = useState<"chat" | "schedules">("chat");
  const [taskDialog, setTaskDialog] = useState(false);
  const current = useRef(app);
  current.current = app;
  const [sidebar, setSidebar] = useState(true);
  const [modal, setModal] = useState<
    | "settings"
    | "folder"
    | "delete-folder"
    | "rename"
    | "move"
    | "delete-chat"
    | "quit"
    | null
  >(null);
  const [folder, setFolder] = useState<Folder>();
  const [deleteTarget, setDeleteTarget] = useState<ChatSummary | null>(null);
  const [name, setName] = useState("");
  const [moveTo, setMoveTo] = useState("");
  const [quitError, setQuitError] = useState(false);
  // Quitting to restart into an installed update.
  const [restarting, setRestarting] = useState(false);
  const [working, setWorking] = useState(false);
  const model = app.models.find(
    (m) => m.name === app.chat?.model && m.available,
  );
  const appearance = app.preferences.appearance ?? "system";
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const apply = () =>
      (document.documentElement.dataset.theme =
        appearance === "system"
          ? media.matches
            ? "dark"
            : "light"
          : String(appearance));
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [appearance]);
  useEffect(() => {
    if (typeof app.preferences.sidebar === "boolean")
      setSidebar(app.preferences.sidebar);
  }, [app.preferences.sidebar]);
  function toggleSidebar() {
    setSidebar((old) => {
      void app.preference("sidebar", !old);
      return !old;
    });
  }
  async function quit(force = false, restart = false) {
    const controller = current.current;
    if (!force) setRestarting(restart);
    if (!force && (controller.active || controller.sending)) {
      setQuitError(false);
      setModal("quit");
      return;
    }
    try {
      if (!force) await controller.flushDrafts();
      await native(restart ? "restart" : "quit");
    } catch {
      setQuitError(true);
      setModal("quit");
    }
  }
  useEffect(() => {
    if (!desktop) return;
    let live = true;
    const offs: (() => void)[] = [];
    for (const [event, fn] of [
      ["quit-requested", () => void quit()],
      [
        "files-dropped",
        () => void current.current.importAttachments("import_drop"),
      ],
    ] as const) {
      void onNative(event, fn).then((off) => {
        if (live) offs.push(off);
        else off();
      });
    }
    return () => {
      live = false;
      offs.forEach((off) => off());
    };
  }, []);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (!e.metaKey) return;
      if (e.key.toLowerCase() === "n" && !modal) {
        e.preventDefault();
        if (!app.offline) {
          setView("chat");
          void app.newChat();
        }
      }
      if (e.key.toLowerCase() === "b" && !modal) {
        e.preventDefault();
        toggleSidebar();
      }
      if (e.key === ",") {
        e.preventDefault();
        setModal("settings");
      }
    };
    addEventListener("keydown", key);
    return () => removeEventListener("keydown", key);
  });
  function editFolder(value?: Folder) {
    setFolder(value);
    setName(value?.name ?? "");
    setModal("folder");
  }
  function confirmDelete(chat: ChatSummary) {
    setDeleteTarget(chat);
    setModal("delete-chat");
  }
  async function action(fn: () => Promise<unknown>) {
    setWorking(true);
    try {
      await fn();
    } finally {
      setWorking(false);
    }
  }
  const rawThinking = app.chat?.thinking[app.chat.model ?? ""] ?? null;
  const thinking = model?.thinking.some(
    (v) => typeof v === typeof rawThinking && v === rawThinking,
  )
    ? rawThinking
    : null;
  // The saved default (null) is "on", so an explicit "on" shows as the same choice.
  const thinkingOnValue = model ? thinkingOn(model) : null;
  const thinkingChoice = thinking === thinkingOnValue ? null : thinking;

  return (
    <div className={`app-shell ${sidebar ? "" : "sidebar-hidden"}`}>
      {sidebar && (
        <Sidebar
          app={app}
          onSettings={() => setModal("settings")}
          onFolder={editFolder}
          onClose={() => setSidebar(false)}
          onDeleteChat={confirmDelete}
          scheduled={view === "schedules"}
          taskCount={schedules.tasks.length}
          onScheduled={() => {
            setView("schedules");
            void schedules.openTask(null);
          }}
          onChat={() => setView("chat")}
        />
      )}
      {sidebar && (
        <button
          className="sidebar-scrim"
          aria-label="Close sidebar"
          onClick={() => setSidebar(false)}
        />
      )}
      {view === "schedules" && (
        <main className="main-pane">
          <Schedules
            schedules={schedules}
            models={app.models}
            defaultModel={
              app.chat?.model ??
              (typeof app.preferences.last_model === "string"
                ? app.preferences.last_model
                : null)
            }
            disabled={app.offline}
            onDialog={setTaskDialog}
            onOpenChat={(id) => {
              setView("chat");
              void app.select(id);
            }}
            sidebarToggle={
              <button
                className="icon-button sidebar-toggle"
                aria-label={sidebar ? "Hide sidebar" : "Show sidebar"}
                title="Toggle sidebar (⌘B)"
                onClick={toggleSidebar}
              >
                <PanelLeft size={19} />
              </button>
            }
          />
        </main>
      )}
      {/* Hidden, not removed, so the chat keeps its scroll and state. */}
      <main className="main-pane" hidden={view === "schedules"}>
        <header className="conversation-header">
          <button
            className="icon-button sidebar-toggle"
            aria-label={sidebar ? "Hide sidebar" : "Show sidebar"}
            title="Toggle sidebar (⌘B)"
            onClick={toggleSidebar}
          >
            <PanelLeft size={19} />
          </button>
          <button
            className="chat-title"
            disabled={!app.chat}
            onClick={() => {
              setName(app.chat?.title ?? "");
              setModal("rename");
            }}
            title="Rename chat"
          >
            {app.chat?.title ?? "Your space to think"}
          </button>
          <div className="model-controls">
            {app.chat && (
              <>
                <div className="select-wrap">
                  <label htmlFor="model">Model</label>
                  <select
                    id="model"
                    title={app.chat.model ?? undefined}
                    value={app.chat.model ?? ""}
                    disabled={app.offline || app.loading || app.sending}
                    onChange={(e) =>
                      void app.updateChat({ model: e.target.value })
                    }
                  >
                    {!app.chat.model && (
                      <option value="" disabled>
                        Select a local model
                      </option>
                    )}
                    {app.chat.model &&
                      !app.models.some((m) => m.name === app.chat?.model) && (
                        <option value={app.chat.model} disabled>
                          {formatModelName(app.chat.model)} · unavailable
                        </option>
                      )}
                    {app.models.map((m) => (
                      <option
                        key={m.name}
                        value={m.name}
                        disabled={!m.available}
                      >
                        {formatModelName(m.name)}
                        {m.vision ? " · images" : ""}
                        {!m.available ? " · unavailable" : ""}
                      </option>
                    ))}
                  </select>
                </div>
                {model && model.thinking.some((v) => v !== thinkingOnValue) && (
                  <div className="select-wrap thinking-select">
                    <label htmlFor="thinking">Thinking</label>
                    <select
                      id="thinking"
                      value={JSON.stringify(thinkingChoice)}
                      onChange={(e) => {
                        const value = JSON.parse(e.target.value) as Thinking;
                        void app.updateChat({
                          thinking: {
                            ...app.chat!.thinking,
                            [model.name]: value,
                          },
                        });
                      }}
                      disabled={app.sending}
                    >
                      <option value="null">
                        {typeof thinkingOnValue === "string"
                          ? `On · ${thinkingOnValue}`
                          : "On"}
                      </option>
                      {model.thinking
                        .filter((value) => value !== thinkingOnValue)
                        .map((value) => (
                          <option
                            value={JSON.stringify(value)}
                            key={JSON.stringify(value)}
                          >
                            {typeof value === "boolean"
                              ? value
                                ? "On"
                                : "Off"
                              : value}
                          </option>
                        ))}
                    </select>
                  </div>
                )}
                <details className="chat-actions">
                  <summary className="icon-button" aria-label="Chat actions">
                    <MoreHorizontal size={20} />
                  </summary>
                  <div className="popover">
                    <button
                      onClick={(e) => {
                        e.currentTarget
                          .closest("details")
                          ?.removeAttribute("open");
                        setName(app.chat!.title);
                        setModal("rename");
                      }}
                    >
                      <Pencil size={14} />
                      Rename chat
                    </button>
                    <button
                      onClick={(e) => {
                        e.currentTarget
                          .closest("details")
                          ?.removeAttribute("open");
                        setMoveTo(app.chat?.folder_id ?? "");
                        setModal("move");
                      }}
                    >
                      <FolderInput size={14} />
                      Move to folder
                    </button>
                    <button
                      className="danger-text"
                      disabled={
                        app.active?.chat_id === app.chat.id ||
                        app.sending ||
                        app.importing ||
                        app.offline ||
                        app.loading
                      }
                      onClick={(e) => {
                        e.currentTarget
                          .closest("details")
                          ?.removeAttribute("open");
                        confirmDelete(app.chat!);
                      }}
                    >
                      <Trash2 size={14} />
                      Delete chat
                    </button>
                  </div>
                </details>
              </>
            )}
          </div>
        </header>
        {app.error && (
          <div className="notice" role="alert">
            <span>{app.error}</span>
            {desktop && (
              <button
                disabled={
                  app.loading || !!app.active || app.sending || app.importing
                }
                onClick={() => void app.reconnect()}
              >
                Reconnect
              </button>
            )}
            <button
              className="icon-button"
              aria-label="Dismiss notice"
              onClick={() => app.setError(null)}
            >
              <X size={15} />
            </button>
          </div>
        )}
        {updates.offer && (
          <div className="notice" role="status">
            <span>Vikram {updates.offer} is installed. Restart to use it.</span>
            <button onClick={() => void quit(false, true)}>Restart</button>
            <button
              className="icon-button"
              aria-label="Dismiss update notice"
              onClick={updates.dismiss}
            >
              <X size={15} />
            </button>
          </div>
        )}
        {app.connection && !app.offline && (
          <div className="notice">
            <span>{app.connection}</span>
            <button
              onClick={() => void app.refreshModels()}
              disabled={app.refreshing}
            >
              Refresh
            </button>
          </div>
        )}
        {app.chat &&
          model &&
          !model.vision &&
          app.chat.attachments.some(
            (a) => a.message_id && a.kind !== "document",
          ) && (
            <div className="context-notice">
              This model sees text only. Saved images stay in your chat and are
              omitted from its context.
            </div>
          )}
        {app.loading && !app.chat ? (
          <div className="empty-state">
            <span className="spinner" />
            <p>Opening your conversations…</p>
          </div>
        ) : app.chat ? (
          <>
            {app.chat.messages.length ? (
              <Transcript
                chat={app.chat}
                disabled={!!app.active || app.sending || app.offline || !model}
                onRetry={() => void app.send(true)}
                onRespond={app.respond}
                onHandBack={() => void app.controlBrowser("agent")}
                onOpenTask={(id) => {
                  setView("schedules");
                  void schedules.openTask(id);
                }}
              />
            ) : (
              <div className="empty-state">
                <div className="empty-copy">
                  <span className="empty-mark">v.</span>
                  <h1>A thought starts here.</h1>
                  <p>
                    Ask a question. Explore an idea.
                    <br />
                    Make a little room for what comes next.
                  </p>
                  <div className="empty-detail">
                    {model ? (
                      <>
                        <span className="local-dot" />
                        {formatModelName(model.name)}
                        {model.vision ? " · text & images" : " · text"}
                      </>
                    ) : (
                      "Choose a model above to start a conversation."
                    )}
                  </div>
                  {!app.models.some((m) => m.available) && (
                    <div className="empty-models">
                      <p>
                        {app.connection
                          ? "Open Ollama to see your local models."
                          : "Install a chat model in Ollama, then refresh. Cloud models are unavailable here."}
                      </p>
                      <button
                        className="secondary"
                        disabled={app.refreshing || app.offline}
                        onClick={() => void app.refreshModels()}
                      >
                        <RefreshCw size={14} />
                        Refresh models
                      </button>
                    </div>
                  )}
                </div>
              </div>
            )}
            <Composer app={app} />
          </>
        ) : (
          <div className="empty-state welcome">
            <div className="empty-copy">
              <span className="empty-mark">v.</span>
              <h1>A thought starts here.</h1>
              <p>
                Your models. Your conversations.
                <br />A quiet space, right on your Mac.
              </p>
              {desktop ? (
                <button
                  className="primary"
                  disabled={app.offline || app.loading}
                  onClick={() => void app.newChat()}
                >
                  Start a conversation
                  <ArrowUpRight size={16} />
                </button>
              ) : (
                <p className="preview-note">
                  Open the Vikram desktop app to connect to your local models.
                </p>
              )}
            </div>
          </div>
        )}
      </main>
      {browser.page.open && (
        <BrowserPanel
          page={browser.page}
          control={app.browserControl}
          phase={app.phase}
          running={!!app.active}
          covered={modal !== null || taskDialog}
          onControl={(control) => void app.controlBrowser(control)}
          onStop={() => void app.stop()}
          onClose={() =>
            void browser
              .close()
              .catch(() =>
                app.setError("The browser could not be closed. Try again."),
              )
          }
        />
      )}
      {modal === "settings" && (
        <Dialog title="Settings" onClose={() => setModal(null)}>
          <section className="settings-section">
            <h3>Appearance</h3>
            <p>Make yourself at home.</p>
            <div className="appearance-options">
              {["system", "light", "dark"].map((theme) => (
                <button
                  key={theme}
                  aria-pressed={appearance === theme}
                  className={appearance === theme ? "chosen" : ""}
                  onClick={() => void app.preference("appearance", theme)}
                >
                  <span className={`theme-swatch swatch-${theme}`} />
                  {theme[0].toUpperCase() + theme.slice(1)}
                </button>
              ))}
            </div>
          </section>
          <section className="settings-section">
            <h3>Web search</h3>
            <p className="muted">
              Models that support tools can search and read public pages through
              Parallel’s free service whenever needed, subject to rate limits.
              The model decides when to search. Queries may include conversation
              or attachment details. Original files stay on your Mac.
            </p>
            <h3>Local models</h3>
            <p>Connected through Ollama on this Mac.</p>
            <div className="connection-row">
              <span
                className={`local-dot ${app.connection ? "unavailable" : ""}`}
              />
              <code>localhost:11434</code>
              <button
                className="secondary"
                disabled={app.refreshing || app.offline}
                onClick={() => void app.refreshModels()}
              >
                <RefreshCw size={13} className={app.refreshing ? "spin" : ""} />
                Refresh
              </button>
            </div>
            <p className="muted">
              {app.connection ??
                `${app.models.filter((m) => m.available).length} local chat models available.`}
            </p>
            {desktop && (
              <button
                className="secondary"
                disabled={
                  app.loading || !!app.active || app.sending || app.importing
                }
                onClick={() => void app.reconnect()}
              >
                Reconnect chat service
              </button>
            )}
            <details className="model-info">
              <summary>Model capabilities</summary>
              <ul>
                {app.models.map((m) => (
                  <li key={m.name}>
                    <strong>{formatModelName(m.name)}</strong>
                    <code className="model-id">{m.name}</code>
                    <span>
                      {!m.available
                        ? m.reason
                        : `${m.vision ? "Text and images" : "Text"}${m.can_think ? " · thinking" : ""}`}
                    </span>
                    {m.available && m.can_think && !m.thinking.length && (
                      <small>
                        Ollama does not advertise adjustable thinking levels.
                        Model default is used.
                      </small>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          </section>
          <section className="settings-section">
            <h3>Private by design</h3>
            <p>
              Chats and imported files are saved on this Mac. There is no cloud
              sync or analytics. The model can read attached content and use web
              search, but has no direct access to your files.
            </p>
            <p className="muted">
              Vikram’s browser has its own private session, with none of your
              other browsers’ cookies or passwords; closing it or quitting
              Vikram clears it. Vikram asks before it submits a form, signs in,
              buys, posts, or deletes, never types passwords or payment details,
              and cannot reach your local network. Take over the browser
              whenever you want to use the page yourself.
            </p>
            <p className="muted">
              Your full conversation is saved. A model’s available context may
              cover less of it; Vikram does not automatically summarize or
              enlarge that context.
            </p>
          </section>
          <div className="settings-footer">
            <span>
              Vikram <span>{version}</span>
            </span>
            {updates.status && updates.status.state !== "unavailable" && (
              <span className="update-line">
                {updateText(updates.status)}
                {updates.status.state === "ready" ? (
                  <button
                    className="link-button"
                    onClick={() => void quit(false, true)}
                  >
                    Restart
                  </button>
                ) : (
                  !["checking", "installing"].includes(
                    updates.status.state,
                  ) && (
                    <button
                      className="link-button"
                      onClick={() => void updates.check()}
                    >
                      Check for updates
                    </button>
                  )
                )}
              </span>
            )}
          </div>
        </Dialog>
      )}
      {modal === "folder" && (
        <Dialog
          title={folder ? "Edit folder" : "New folder"}
          onClose={() => setModal(null)}
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void action(async () => {
                if (
                  await app.manage(
                    folder ? "rename_folder" : "create_folder",
                    folder ? { folder_id: folder.id, name } : { name },
                  )
                )
                  setModal(null);
              });
            }}
          >
            <label className="field-label" htmlFor="folder-name">
              Folder name
            </label>
            <input
              id="folder-name"
              autoFocus
              maxLength={80}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Ideas, Work, Learning"
            />
            <div className="dialog-actions">
              {folder && (
                <button
                  type="button"
                  className="danger-text push-left"
                  onClick={() => setModal("delete-folder")}
                >
                  Delete folder
                </button>
              )}
              <button
                type="button"
                className="secondary"
                onClick={() => setModal(null)}
              >
                Cancel
              </button>
              <button className="primary" disabled={!name.trim() || working}>
                {folder ? "Save" : "Create folder"}
              </button>
            </div>
          </form>
        </Dialog>
      )}
      {modal === "delete-folder" && folder && (
        <Dialog title="Delete folder?" onClose={() => setModal(null)}>
          <p>
            “{folder.name}” will be removed. Its chats will move to Unfiled.
          </p>
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setModal(null)}>
              Cancel
            </button>
            <button
              className="danger-button"
              disabled={working}
              onClick={() =>
                void action(async () => {
                  if (
                    await app.manage("delete_folder", { folder_id: folder.id })
                  )
                    setModal(null);
                })
              }
            >
              Delete folder
            </button>
          </div>
        </Dialog>
      )}
      {modal === "rename" && (
        <Dialog title="Rename chat" onClose={() => setModal(null)}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void app.updateChat({ title: name }).then(() => setModal(null));
            }}
          >
            <label className="field-label" htmlFor="chat-name">
              Chat title
            </label>
            <input
              id="chat-name"
              autoFocus
              maxLength={160}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <div className="dialog-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => setModal(null)}
              >
                Cancel
              </button>
              <button className="primary" disabled={!name.trim()}>
                Save
              </button>
            </div>
          </form>
        </Dialog>
      )}
      {modal === "move" && (
        <Dialog title="Move chat" onClose={() => setModal(null)}>
          <label className="field-label" htmlFor="move-folder">
            Folder
          </label>
          <select
            id="move-folder"
            value={moveTo}
            onChange={(e) => setMoveTo(e.target.value)}
          >
            <option value="">Unfiled</option>
            {app.folders.map((f) => (
              <option key={f.id} value={f.id}>
                {f.name}
              </option>
            ))}
          </select>
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setModal(null)}>
              Cancel
            </button>
            <button
              className="primary"
              onClick={() =>
                void app
                  .updateChat({ folder_id: moveTo || null })
                  .then(() => setModal(null))
              }
            >
              Move chat
            </button>
          </div>
        </Dialog>
      )}
      {modal === "delete-chat" && deleteTarget && (
        <Dialog title="Delete this chat?" onClose={() => setModal(null)}>
          <p>
            “{deleteTarget.title}”, its messages, and its imported file copies
            will be permanently removed. Your original files will stay where
            they are.
          </p>
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setModal(null)}>
              Cancel
            </button>
            <button
              className="danger-button"
              disabled={
                working ||
                app.offline ||
                app.sending ||
                app.importing ||
                app.active?.chat_id === deleteTarget.id
              }
              onClick={() =>
                void action(async () => {
                  if (await app.deleteChat(deleteTarget.id)) setModal(null);
                })
              }
            >
              Delete chat
            </button>
          </div>
        </Dialog>
      )}
      {modal === "quit" && (
        <Dialog
          title={
            quitError
              ? `${restarting ? "Restart" : "Quit"} with unsaved changes?`
              : `Stop the reply and ${restarting ? "restart" : "quit"}?`
          }
          onClose={() => setModal(null)}
        >
          <p>
            {quitError
              ? `Your latest draft could not be saved. Copy it before ${restarting ? "restarting" : "quitting"} to keep your text.`
              : "The active reply will stop. Saved messages and its last saved partial answer will be here when you return."}
          </p>
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setModal(null)}>
              Keep open
            </button>
            <button
              className="primary"
              onClick={() =>
                void action(async () => {
                  if (!quitError) {
                    try {
                      await app.flushDrafts();
                    } catch {
                      setQuitError(true);
                      return;
                    }
                  }
                  await quit(true, restarting);
                })
              }
            >
              {quitError
                ? `${restarting ? "Restart" : "Quit"} without saving`
                : `Stop and ${restarting ? "restart" : "quit"}`}
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}

function updateText(status: UpdateStatus): string {
  switch (status.state) {
    case "checking":
      return "Checking for updates…";
    case "installing":
      return `Updating to ${status.version}…`;
    case "current":
      return "Up to date.";
    case "ready":
      return `${status.version} is installed.`;
    case "failed":
      return status.error ?? "Could not check for updates.";
    default:
      return "";
  }
}
