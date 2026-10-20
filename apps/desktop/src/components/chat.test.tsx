// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Transcript } from "./Transcript";
import { Composer } from "./Composer";
import type { Chat } from "../types";
import type { ChatController } from "../useChats";

vi.mock("../bridge", () => ({
  copyText: vi.fn(),
  openLink: vi.fn(),
  rpc: vi.fn(),
}));
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

it("keeps thinking collapsed, then reveals it separately from the answer", () => {
  const chat = {
    id: "a",
    attachments: [],
    messages: [
      {
        id: "m",
        text: "Hello",
        attempts: [
          {
            id: "x",
            requested_model: "local:1",
            actual_model: "local:1",
            status: "complete",
            content: "The answer",
            reasoning: "Model reasoning",
            reasoning_truncated: true,
          },
        ],
      },
    ],
  } as unknown as Chat;
  render(<Transcript chat={chat} disabled={false} onRetry={vi.fn()} />);
  expect(screen.queryByText("Model reasoning")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Thinking" }));
  expect(screen.getByText("Model reasoning")).toBeTruthy();
  expect(screen.getByText(/display limit/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Copy thinking" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Copy reply" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Thinking" }));
  expect(screen.queryByText("Model reasoning")).toBeNull();
});

it("distinguishes waiting for Ollama from actual model thinking", () => {
  const chat = {
    id: "a",
    attachments: [],
    messages: [
      {
        id: "m",
        text: "Hello",
        attempts: [
          {
            id: "x",
            status: "running",
            requested_model: "local:1",
            actual_model: null,
            content: "",
            phase: "loading",
          },
        ],
      },
    ],
  } as unknown as Chat;
  const view = render(
    <Transcript chat={chat} disabled={false} onRetry={vi.fn()} />,
  );
  expect(screen.getByRole("status").textContent).toContain(
    "Waiting for Ollama",
  );
  chat.messages[0].attempts[0].phase = "thinking";
  view.rerender(<Transcript chat={chat} disabled={false} onRetry={vi.fn()} />);
  expect(screen.getByRole("status").textContent).toContain("Thinking");
  chat.messages[0].attempts[0].reasoning = "Live model thinking";
  view.rerender(<Transcript chat={chat} disabled={false} onRetry={vi.fn()} />);
  expect(screen.queryByRole("status")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Thinking…" }));
  expect(screen.getByText("Live model thinking")).toBeTruthy();
  chat.messages[0].attempts[0].reasoning += " continues";
  view.rerender(<Transcript chat={chat} disabled={false} onRetry={vi.fn()} />);
  expect(screen.getByText("Live model thinking continues")).toBeTruthy();
});

it("shows web activity even after the model has written intermediate text", () => {
  const chat = {
    id: "a",
    attachments: [],
    messages: [
      {
        id: "m",
        text: "Find docs",
        attempts: [
          {
            id: "x",
            status: "running",
            requested_model: "local:1",
            actual_model: null,
            content: "Checking…",
            phase: "searching",
          },
        ],
      },
    ],
  } as unknown as Chat;
  const view = render(
    <Transcript chat={chat} disabled={false} onRetry={vi.fn()} />,
  );
  expect(screen.getByRole("status").textContent).toContain("Searching the web");
  chat.messages[0].attempts[0].phase = "fetching";
  view.rerender(<Transcript chat={chat} disabled={false} onRetry={vi.fn()} />);
  expect(screen.getByRole("status").textContent).toContain("Reading sources");
});

it("allows a document-only message with a text-only model", () => {
  const chat = {
    id: "a",
    model: "text",
    attachments: [
      {
        id: "d",
        name: "notes.txt",
        kind: "document",
        message_id: null,
        extracted_text: "Notes",
      },
    ],
  };
  const app = {
    chat,
    draft: "",
    models: [{ name: "text", available: true, vision: false, tools: false }],
    changeDraft: vi.fn(),
    removeAttachment: vi.fn(),
  } as unknown as ChatController;
  render(<Composer app={app} />);
  expect(
    (screen.getByRole("button", { name: "Send message" }) as HTMLButtonElement)
      .disabled,
  ).toBe(false);
  expect(screen.getByText(/Web unavailable/)).toBeTruthy();
});
