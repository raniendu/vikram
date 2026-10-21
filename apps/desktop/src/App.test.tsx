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

const state = vi.hoisted(() => ({ app: null as unknown as ChatController }));
vi.mock("./useChats", () => ({ useChats: () => state.app }));
vi.mock("./bridge", () => ({
  desktop: false,
  native: vi.fn(),
  onNative: vi.fn(),
}));
import App from "./App";

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
  } as unknown as ChatController;
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
