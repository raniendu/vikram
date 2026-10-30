// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Chat } from "./types";
import type { ChatController } from "./useChats";
import type { useUpdates } from "./useUpdates";

type UpdateController = ReturnType<typeof useUpdates>;

const state = vi.hoisted(() => ({
  app: null as unknown as ChatController,
  updates: null as unknown as UpdateController,
}));
vi.mock("./useChats", () => ({ useChats: () => state.app }));
vi.mock("./useUpdates", () => ({ useUpdates: () => state.updates }));
vi.mock("./bridge", () => ({
  desktop: false,
  native: vi.fn(),
  onNative: vi.fn(),
}));
import App from "./App";
import { native } from "./bridge";

beforeEach(() => {
  window.matchMedia = vi
    .fn()
    .mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  const chat = {
    id: "a",
    title: "Current chat",
    model: null,
    thinking: {},
    messages: [],
    attachments: [],
    folder_id: null,
  } as unknown as Chat;
  state.app = {
    chat,
    selected: "a",
    chats: [chat, { ...chat, id: "b", title: "Old chat" }],
    folders: [],
    models: [],
    preferences: {},
    deleteChat: vi.fn().mockResolvedValue(true),
    select: vi.fn(),
    draft: "",
    flushDrafts: vi.fn().mockResolvedValue(undefined),
  } as unknown as ChatController;
  state.updates = {
    status: null,
    offer: null,
    dismiss: vi.fn(),
    check: vi.fn(),
  };
  vi.mocked(native).mockReset();
});
afterEach(cleanup);

it("offers deletion beside every chat and confirms the captured target", async () => {
  const view = render(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Delete Old chat" }));
  const dialog = screen.getByRole("dialog", { name: "Delete this chat?" });
  expect(
    within(dialog).getByText(/Old chat.*permanently removed/),
  ).toBeTruthy();
  expect(state.app.select).not.toHaveBeenCalled();
  expect(state.app.deleteChat).not.toHaveBeenCalled();
  state.app = {
    ...state.app,
    chat: { ...state.app.chat!, title: "Changed selection" },
  };
  view.rerender(<App />);
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete chat" }));
  await waitFor(() => expect(state.app.deleteChat).toHaveBeenCalledWith("b"));
});

it("cancels deletion without modifying a conversation", () => {
  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Delete Old chat" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(state.app.deleteChat).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("disables deletion for the running chat while leaving other chats available", () => {
  state.app = {
    ...state.app,
    active: { chat_id: "a", attempt_id: "x", request_id: "r" },
  };
  render(<App />);
  expect(
    (
      screen.getByRole("button", {
        name: "Delete Current chat",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  expect(
    (
      screen.getByRole("button", {
        name: "Delete Old chat",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(false);
});

it("defaults thinking to on and offers off only as an explicit choice", () => {
  const updateChat = vi.fn();
  const model = {
    name: "local:1",
    available: true,
    vision: false,
    tools: true,
    can_think: true,
    thinking: [false, true],
    thinking_default: true,
    reason: null,
    size: 1,
    context_length: null,
  };
  state.app = {
    ...state.app,
    chat: { ...state.app.chat!, model: "local:1", thinking: {} },
    models: [model],
    updateChat,
  };
  const view = render(<App />);
  const select = screen.getByLabelText("Thinking") as HTMLSelectElement;
  expect(select.selectedOptions[0].textContent).toBe("On");
  expect([...select.options].map((o) => o.textContent)).toEqual(["On", "Off"]);
  fireEvent.change(select, { target: { value: "false" } });
  expect(updateChat).toHaveBeenCalledWith({ thinking: { "local:1": false } });
  // An explicit "on" saved earlier is the same choice as the default.
  state.app = {
    ...state.app,
    chat: { ...state.app.chat!, thinking: { "local:1": true } },
  };
  view.rerender(<App />);
  expect(
    (screen.getByLabelText("Thinking") as HTMLSelectElement).selectedOptions[0]
      .textContent,
  ).toBe("On");
});

it("switches between a chat and the scheduled tasks", () => {
  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: /Scheduled/ }));
  expect(
    screen.getByRole("heading", { name: "Scheduled tasks" }),
  ).toBeTruthy();
  expect(screen.getByText("Nothing scheduled yet.")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Old chat" }));
  expect(state.app.select).toHaveBeenCalledWith("b");
  expect(screen.queryByRole("heading", { name: "Scheduled tasks" })).toBeNull();
});

it("offers a restart into an installed update", async () => {
  state.updates = {
    ...state.updates,
    status: { state: "ready", running: "0.3.0", version: "0.4.0", error: null },
    offer: "0.4.0",
  };
  render(<App />);
  expect(
    screen.getByText("Vikram 0.4.0 is installed. Restart to use it."),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Restart" }));
  await waitFor(() => expect(native).toHaveBeenCalledWith("restart"));
  expect(state.app.flushDrafts).toHaveBeenCalled();
});

it("asks before a restart stops a running reply", async () => {
  state.app = {
    ...state.app,
    active: { chat_id: "a" },
  } as unknown as ChatController;
  state.updates = { ...state.updates, offer: "0.4.0" };
  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Restart" }));
  const dialog = screen.getByRole("dialog", {
    name: "Stop the reply and restart?",
  });
  expect(native).not.toHaveBeenCalled();
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Stop and restart" }),
  );
  await waitFor(() => expect(native).toHaveBeenCalledWith("restart"));
});

it("shows update progress in settings", () => {
  state.updates = {
    ...state.updates,
    status: {
      state: "installing",
      running: "0.3.0",
      version: "0.4.0",
      error: null,
    },
  };
  const view = render(<App />);
  fireEvent.keyDown(window, { key: ",", metaKey: true });
  expect(screen.getByText("Updating to 0.4.0…")).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Check for updates" }),
  ).toBeNull();
  state.updates = {
    ...state.updates,
    status: { state: "current", running: "0.3.0", version: null, error: null },
  };
  view.rerender(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Check for updates" }));
  expect(state.updates.check).toHaveBeenCalled();
});
