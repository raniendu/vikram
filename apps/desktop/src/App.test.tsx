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
