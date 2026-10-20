import { useEffect, useRef, useState } from "react";
import { desktop, native, onRuntime, rpc } from "./bridge";
import {
  applyAttempt,
  reconcileActive,
  recoverChat,
  recoverDraft,
  type Submission,
} from "./state";
import type {
  Active,
  ActivityRecord,
  Attempt,
  Chat,
  ChatSummary,
  Folder,
  LocalModel,
  Reply,
  RuntimeEvent,
  Snapshot,
} from "./types";

export function useChats() {
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [models, setModels] = useState<LocalModel[]>([]);
  const [cache, setCache] = useState<Record<string, Chat>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [active, setActive] = useState<Active | null>(null);
  const [preferences, setPreferences] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<string | null>(null);
  const [offline, setOffline] = useState(!desktop);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [importing, setImporting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const draftRef = useRef(
    new Map<string, { text: string; revision: number }>(),
  );
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const events = useRef(new Map<string, RuntimeEvent>());
  const submissions = useRef(new Map<string, Submission>());
  const eventVersion = useRef(0);
  const sendLock = useRef(false);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  const fail = (e: unknown) =>
    setError(
      typeof e === "string"
        ? e
        : "This action could not finish. Please try again.",
    );
  function putChat(chat: Chat, recovery = false) {
    let merged = recovery ? recoverChat(chat, events.current) : chat;
    if (recovery) {
      for (const message of merged.messages)
        for (const attempt of message.attempts) {
          if (!attempt.unsaved) events.current.delete(attempt.id);
        }
    } else
      for (const event of events.current.values())
        merged = applyAttempt(merged, event);
    setCache((old) => ({ ...old, [chat.id]: merged }));
    setChats((old) =>
      [chat, ...old.filter((c) => c.id !== chat.id)].sort(
        (a, b) => b.updated - a.updated,
      ),
    );
    if (!draftRef.current.has(chat.id)) {
      draftRef.current.set(chat.id, {
        text: chat.draft,
        revision: chat.draft_revision,
      });
      setDrafts((old) => ({ ...old, [chat.id]: chat.draft }));
    }
  }
  async function loadChat(id: string, recovery = false) {
    const chat = await rpc<Chat>("chat", { chat_id: id });
    putChat(chat, recovery);
    return chat;
  }
  async function snapshot() {
    const version = eventVersion.current;
    const state = await rpc<Snapshot>("snapshot");
    setChats(state.chats);
    setFolders(state.folders);
    if (eventVersion.current === version)
      setActive(reconcileActive(state.active, events.current));
    return state;
  }
  async function refreshModels() {
    setRefreshing(true);
    try {
      const state = await rpc<{
        models: LocalModel[];
        connection_error: string | null;
      }>("refresh");
      setModels(state.models);
      setConnection(state.connection_error);
    } catch (e) {
      fail(e);
    } finally {
      setRefreshing(false);
    }
  }
  async function boot(reconnect = false) {
    setLoading(true);
    try {
      const state = reconnect
        ? await native<Snapshot>("reconnect")
        : await rpc<Snapshot>("hello");
      if (state.protocol !== 1)
        throw "The desktop service version does not match this app. Rebuild or update Vikram.";
      setChats(state.chats);
      setFolders(state.folders);
      setActive(reconcileActive(state.active, events.current));
      setPreferences(state.preferences);
      setOffline(false);
      const previous = selectedRef.current ?? state.preferences.selected_chat;
      const id = state.chats.some((c) => c.id === previous)
        ? (previous as string)
        : state.chats[0]?.id;
      if (id) {
        setSelected(id);
        await loadChat(id, reconnect);
      }
      await refreshModels();
      // Local drafts survive a sidecar restart; rebase their revisions on saved records.
      if (reconnect) {
        for (const chat of state.chats) {
          if (draftRef.current.has(chat.id)) {
            const saved = await rpc<Chat>("chat", { chat_id: chat.id });
            const draft = draftRef.current.get(chat.id);
            if (!draft) continue;
            const recovered = recoverDraft(
              saved,
              draft,
              submissions.current.get(chat.id),
            );
            draftRef.current.set(chat.id, recovered);
            setDrafts((old) => ({ ...old, [chat.id]: recovered.text }));
            submissions.current.delete(chat.id);
            putChat(saved, true);
            await rpc("draft", {
              chat_id: chat.id,
              text: recovered.text,
              revision: recovered.revision,
            });
          }
        }
      }
    } catch (e) {
      fail(e);
      setOffline(true);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    if (!desktop) {
      setLoading(false);
      return;
    }
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void onRuntime((event) => {
      eventVersion.current += 1;
      if (
        event.event === "attempt" &&
        event.attempt_id &&
        event.chat_id &&
        event.request_id
      ) {
        const previous = events.current.get(event.attempt_id);
        if (
          previous?.status !== undefined &&
          previous.status !== "running" &&
          event.status === "running"
        )
          return;
        events.current.set(event.attempt_id, event);
        setCache((old) =>
          old[event.chat_id!]
            ? {
                ...old,
                [event.chat_id!]: applyAttempt(old[event.chat_id!], event),
              }
            : old,
        );
        if (event.status === "running")
          setActive({
            chat_id: event.chat_id,
            attempt_id: event.attempt_id,
            request_id: event.request_id,
          });
      } else if (event.event === "chat_mode" && event.chat_id && event.mode) {
        // An approved plan ends plan mode for the chat's next replies.
        const { chat_id: id, mode } = event;
        setCache((old) =>
          old[id] ? { ...old, [id]: { ...old[id], mode } } : old,
        );
        setChats((old) => old.map((c) => (c.id === id ? { ...c, mode } : c)));
      } else if (event.event === "idle") {
        setActive((old) => (old?.attempt_id === event.attempt_id ? null : old));
      } else if (event.event === "notice") setError(event.message ?? null);
      else if (event.event === "fatal" || event.event === "disconnected") {
        setOffline(true);
        setActive(null);
        for (const [id, event] of events.current) {
          if (event.status === "running")
            events.current.set(id, {
              ...event,
              status: "interrupted",
              unsaved: true,
            });
        }
        setError(
          event.message ??
            "The chat service disconnected. Your visible draft is retained. Reconnect to continue.",
        );
        setCache((old) =>
          Object.fromEntries(
            Object.entries(old).map(([id, chat]) => [
              id,
              {
                ...chat,
                messages: chat.messages.map((m) => ({
                  ...m,
                  attempts: m.attempts.map((a) =>
                    a.status === "running"
                      ? { ...a, status: "interrupted", unsaved: true }
                      : a,
                  ),
                })),
              },
            ]),
          ),
        );
      }
    }).then((off) => {
      unlisten = off;
      if (disposed) off();
      else void boot();
    });
    return () => {
      disposed = true;
      unlisten?.();
      for (const timer of timers.current.values()) clearTimeout(timer);
    };
  }, []);

  async function select(id: string) {
    setSelected(id);
    try {
      await loadChat(id);
      await rpc("preference", { key: "selected_chat", value: id });
    } catch (e) {
      fail(e);
    }
  }
  async function newChat(folder: string | null = null) {
    try {
      const chat = await rpc<Chat>("create_chat", { folder_id: folder });
      putChat(chat);
      await select(chat.id);
    } catch (e) {
      fail(e);
    }
  }
  async function saveDraft(id: string) {
    clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    const draft = draftRef.current.get(id);
    // A send may have consumed this draft even if its acknowledgement was lost.
    if (submissions.current.has(id))
      throw "Reconnect to confirm the last send before saving this draft.";
    if (draft)
      await rpc("draft", {
        chat_id: id,
        text: draft.text,
        revision: draft.revision,
      });
  }
  function changeDraft(text: string) {
    if (!selected) return;
    const id = selected;
    const previous = draftRef.current.get(id);
    draftRef.current.set(id, {
      text,
      revision: (previous?.revision ?? cache[id]?.draft_revision ?? 0) + 1,
    });
    setDrafts((old) => ({ ...old, [id]: text }));
    clearTimeout(timers.current.get(id));
    timers.current.set(
      id,
      setTimeout(() => {
        void saveDraft(id).catch(() =>
          setError(
            "Draft not saved. Keep a copy of your text and reconnect after checking disk space.",
          ),
        );
      }, 250),
    );
  }
  async function flushDrafts() {
    for (const id of draftRef.current.keys()) await saveDraft(id);
  }
  async function updateChat(fields: Record<string, unknown>) {
    if (!selected) return;
    try {
      putChat(await rpc<Chat>("update_chat", { chat_id: selected, fields }));
    } catch (e) {
      fail(e);
    }
  }
  async function send(retry = false) {
    if (!selected || active || sendLock.current || importing) return;
    const id = selected;
    sendLock.current = true;
    setSending(true);
    setError(null);
    try {
      await saveDraft(id);
      const requestId = crypto.randomUUID();
      if (!retry)
        submissions.current.set(id, {
          requestId,
          text: draftRef.current.get(id)?.text ?? "",
          revision: draftRef.current.get(id)?.revision ?? 0,
        });
      const result = await rpc<{
        chat: Chat;
        active: Active;
        models: LocalModel[];
      }>("send", { chat_id: id, retry, request_id: requestId });
      submissions.current.delete(id);
      if (!retry) {
        draftRef.current.set(id, {
          text: result.chat.draft,
          revision: result.chat.draft_revision,
        });
        setDrafts((old) => ({ ...old, [id]: result.chat.draft }));
      }
      putChat(result.chat);
      setActive(reconcileActive(result.active, events.current));
      setModels(result.models);
    } catch (e) {
      fail(e);
      try {
        const saved = await rpc<Chat>("chat", { chat_id: id });
        const draft = draftRef.current.get(id);
        if (draft) {
          const recovered = recoverDraft(
            saved,
            draft,
            submissions.current.get(id),
          );
          draftRef.current.set(id, recovered);
          setDrafts((old) => ({ ...old, [id]: recovered.text }));
        }
        submissions.current.delete(id);
        putChat(saved);
        void refreshModels();
      } catch {
        setOffline(true);
      }
    } finally {
      sendLock.current = false;
      setSending(false);
    }
  }
  async function stop() {
    try {
      await rpc("stop");
    } catch (e) {
      fail(e);
    }
  }
  async function respond(
    attempt: Attempt,
    record: ActivityRecord,
    reply: Reply,
  ) {
    try {
      await rpc("respond", {
        chat_id: attempt.chat_id,
        attempt_id: attempt.id,
        interaction_id: record.id,
        kind: record.kind,
        ...reply,
      });
      return true;
    } catch (e) {
      fail(e);
      return false;
    }
  }
  async function manage(method: string, params: Record<string, unknown>) {
    try {
      await rpc(method, params);
      await snapshot();
      return true;
    } catch (e) {
      fail(e);
      return false;
    }
  }
  async function deleteChat(id = selectedRef.current) {
    if (!id) return false;
    if (!(await manage("delete_chat", { chat_id: id }))) return false;
    clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    draftRef.current.delete(id);
    setCache((old) => {
      const copy = { ...old };
      delete copy[id];
      return copy;
    });
    const state = await snapshot();
    if (selectedRef.current === id) {
      const next = state.chats[0]?.id;
      setSelected(next ?? null);
      if (next) await loadChat(next);
    }
    return true;
  }
  async function importAttachments(
    source: "pick_attachments" | "import_drop" | File[],
  ) {
    const id = selectedRef.current;
    if (!id || importing || sendLock.current) return;
    setImporting(true);
    try {
      const errors: string[] = [];
      if (typeof source === "string") {
        const result = await native<{ errors: string[] }>(source, {
          chatId: id,
        });
        errors.push(...result.errors);
      } else {
        for (const file of source.slice(0, 4)) {
          if (file.size > 10 * 1024 * 1024) {
            errors.push("Files must be no larger than 10 MiB.");
            continue;
          }
          try {
            const data = await new Promise<string>((resolve, reject) => {
              const reader = new FileReader();
              reader.onload = () =>
                resolve(String(reader.result).split(",")[1]);
              reader.onerror = reject;
              reader.readAsDataURL(file);
            });
            await rpc("import_attachment", {
              chat_id: id,
              name: file.name,
              data,
            });
          } catch (e) {
            errors.push(
              typeof e === "string" ? e : "This file could not be imported.",
            );
          }
        }
        if (source.length > 4)
          errors.push("Attach up to four files per message.");
      }
      await loadChat(id);
      if (errors.length) setError([...new Set(errors)].join(" "));
    } catch (e) {
      fail(e);
    } finally {
      setImporting(false);
    }
  }
  async function removeAttachment(aid: string) {
    if (!selected) return;
    try {
      await rpc("remove_attachment", { chat_id: selected, attachment_id: aid });
      await loadChat(selected);
    } catch (e) {
      fail(e);
    }
  }
  async function preference(key: string, value: unknown) {
    setPreferences((old) => ({ ...old, [key]: value }));
    try {
      await rpc("preference", { key, value });
    } catch (e) {
      fail(e);
    }
  }
  return {
    chats,
    folders,
    models,
    chat: selected ? cache[selected] : undefined,
    selected,
    active,
    preferences,
    error,
    setError,
    connection,
    offline,
    loading,
    sending,
    importing,
    refreshing,
    draft: selected ? (drafts[selected] ?? "") : "",
    select,
    newChat,
    updateChat,
    changeDraft,
    send,
    stop,
    respond,
    manage,
    deleteChat,
    importAttachments,
    removeAttachment,
    preference,
    refreshModels,
    reconnect: () => boot(true),
    flushDrafts,
  };
}
export type ChatController = ReturnType<typeof useChats>;
