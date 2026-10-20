// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Chat, RuntimeEvent, Snapshot } from "./types";

const bridge = vi.hoisted(() => ({
  rpc: vi.fn(),
  native: vi.fn(),
  receive: null as ((e: RuntimeEvent) => void) | null,
}));
vi.mock("./bridge", () => ({
  desktop: true,
  rpc: bridge.rpc,
  native: bridge.native,
  onRuntime: async (fn: (e: RuntimeEvent) => void) => {
    bridge.receive = fn;
    return () => {
      bridge.receive = null;
    };
  },
}));
import { useChats } from "./useChats";

let saved: Chat;
const active = {
  chat_id: "a".repeat(32),
  attempt_id: "attempt",
  request_id: "running",
};
function snapshot(): Snapshot {
  return {
    protocol: 1,
    chats: [saved],
    folders: [],
    preferences: {},
    active: null,
    models: [],
  };
}
function receive(event: RuntimeEvent) {
  act(() => bridge.receive?.(event));
}
async function mount() {
  const hook = renderHook(() => useChats());
  await waitFor(() => expect(hook.result.current.loading).toBe(false));
  return hook;
}
beforeEach(() => {
  saved = {
    id: active.chat_id,
    title: "Test chat",
    folder_id: null,
    model: "local",
    thinking: {},
    draft: "",
    draft_revision: 0,
    created: 1,
    updated: 1,
    messages: [],
    attachments: [],
  };
  bridge.rpc.mockReset();
  bridge.native.mockReset();
  bridge.rpc.mockImplementation(async (method: string) => {
    if (method === "hello" || method === "snapshot") return snapshot();
    if (method === "chat") return structuredClone(saved);
    if (method === "refresh") return { models: [], connection_error: null };
    return null;
  });
  bridge.native.mockImplementation(async () => snapshot());
});
afterEach(cleanup);

describe("conversation deletion", () => {
  it.each([false, true])(
    "deletes an explicit target; current chat: %s",
    async (current) => {
      const other = { ...saved, id: "b".repeat(32), title: "Other chat" };
      let chats = [saved, other];
      const original = bridge.rpc.getMockImplementation()!;
      bridge.rpc.mockImplementation(
        async (method: string, params: Record<string, unknown>) => {
          if (method === "hello" || method === "snapshot")
            return { ...snapshot(), chats };
          if (method === "chat")
            return structuredClone(chats.find((c) => c.id === params.chat_id));
          if (method === "delete_chat") {
            chats = chats.filter((c) => c.id !== params.chat_id);
            return null;
          }
          return original(method, params);
        },
      );
      const hook = await mount();
      const target = current ? saved.id : other.id;
      await act(async () => {
        await hook.result.current.deleteChat(target);
      });
      expect(bridge.rpc).toHaveBeenCalledWith("delete_chat", {
        chat_id: target,
      });
      expect(hook.result.current.selected).toBe(current ? other.id : saved.id);
      expect(hook.result.current.chats.map((c) => c.id)).toEqual([
        current ? other.id : saved.id,
      ]);
    },
  );
});

describe("chat controller recovery", () => {
  it("ignores an old active snapshot after terminal events", async () => {
    const hook = await mount();
    let resolve!: (s: Snapshot) => void;
    const original = bridge.rpc.getMockImplementation()!;
    bridge.rpc.mockImplementation((method: string, params?: unknown) =>
      method === "snapshot"
        ? new Promise((r) => {
            resolve = r;
          })
        : original(method, params),
    );
    let management!: Promise<boolean>;
    act(() => {
      management = hook.result.current.manage("create_folder", {
        name: "Work",
      });
    });
    await waitFor(() => expect(resolve).toBeDefined());
    receive({
      v: 1,
      event: "attempt",
      ...active,
      status: "complete",
      content: "done",
    });
    receive({ v: 1, event: "idle", ...active });
    await act(async () => {
      resolve({ ...snapshot(), active });
      await management;
    });
    expect(hook.result.current.active).toBeNull();
  });

  it("reconciles a committed send when its acknowledgement was lost", async () => {
    const hook = await mount();
    act(() => hook.result.current.changeDraft("send once"));
    let disconnected = false;
    const original = bridge.rpc.getMockImplementation()!;
    bridge.rpc.mockImplementation(
      async (method: string, params: Record<string, unknown>) => {
        if (method === "send") {
          saved = {
            ...saved,
            draft: "",
            draft_revision: 2,
            messages: [
              {
                id: "m",
                text: "send once",
                created: 1,
                attempts: [
                  {
                    id: "a",
                    chat_id: saved.id,
                    message_id: "m",
                    request_id: String(params.request_id),
                    requested_model: "local",
                    actual_model: null,
                    thinking: null,
                    content: "",
                    status: "interrupted",
                    error: null,
                  },
                ],
              },
            ],
          };
          disconnected = true;
          throw "Service disconnected";
        }
        if (disconnected) throw "Service disconnected";
        return original(method, params);
      },
    );
    await act(async () => {
      await hook.result.current.send();
    });
    expect(hook.result.current.offline).toBe(true);
    disconnected = false;
    await act(async () => {
      await hook.result.current.reconnect();
    });
    expect(hook.result.current.draft).toBe("");
    expect(hook.result.current.chat?.messages).toHaveLength(1);
    expect(bridge.rpc.mock.calls.filter(([m]) => m === "send")).toHaveLength(1);
    const writes = bridge.rpc.mock.calls.filter(([m]) => m === "draft");
    expect(writes.at(-1)?.[1].text).toBe("");
  });

  it("keeps the newer disk checkpoint after a disconnect", async () => {
    saved.messages = [
      {
        id: "m",
        text: "question",
        created: 1,
        attempts: [
          {
            id: active.attempt_id,
            request_id: active.request_id,
            chat_id: saved.id,
            message_id: "m",
            requested_model: "local",
            actual_model: "local",
            thinking: null,
            status: "running",
            content: "hello",
            error: null,
          },
        ],
      },
    ];
    const hook = await mount();
    receive({
      v: 1,
      event: "attempt",
      ...active,
      content: "hello",
      status: "running",
    });
    receive({ v: 1, event: "disconnected" });
    saved.messages[0].attempts[0] = {
      ...saved.messages[0].attempts[0],
      status: "interrupted",
      content: "hello world saved to disk",
    };
    await act(async () => {
      await hook.result.current.reconnect();
    });
    expect(hook.result.current.chat?.messages[0].attempts[0]).toMatchObject({
      content: "hello world saved to disk",
      status: "interrupted",
    });
  });

  it("refuses to report unsaved drafts as flushed after a lost send acknowledgement", async () => {
    const hook = await mount();
    act(() => hook.result.current.changeDraft("send once"));
    const original = bridge.rpc.getMockImplementation()!;
    let disconnected = false;
    bridge.rpc.mockImplementation(async (method: string, params: unknown) => {
      if (method === "send") disconnected = true;
      if (disconnected) throw "Service disconnected";
      return original(method, params);
    });
    await act(async () => {
      await hook.result.current.send();
    });
    act(() => hook.result.current.changeDraft("new unsaved thought"));
    await expect(hook.result.current.flushDrafts()).rejects.toMatch(
      /reconnect/i,
    );
    expect(hook.result.current.draft).toBe("new unsaved thought");
  });

  it("preserves typing while reconnect waits for the saved chat", async () => {
    saved.draft = "original draft";
    saved.draft_revision = 1;
    const hook = await mount();
    const original = bridge.rpc.getMockImplementation()!;
    let reads = 0;
    let resolve!: (chat: Chat) => void;
    bridge.rpc.mockImplementation((method: string, params: unknown) => {
      if (method === "chat" && ++reads === 2)
        return new Promise((r) => {
          resolve = r;
        });
      return original(method, params);
    });
    let recovery!: Promise<void>;
    act(() => {
      recovery = hook.result.current.reconnect();
    });
    await waitFor(() => expect(resolve).toBeDefined());
    act(() => hook.result.current.changeDraft("typed during reconnect"));
    await act(async () => {
      resolve(structuredClone(saved));
      await recovery;
    });
    expect(hook.result.current.draft).toBe("typed during reconnect");
    const writes = bridge.rpc.mock.calls.filter(([m]) => m === "draft");
    expect(writes.at(-1)?.[1].text).toBe("typed during reconnect");
  });
});
