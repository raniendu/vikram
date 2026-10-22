// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
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

function waitingChat(activity: unknown[], status = "running") {
  return {
    id: "a",
    attachments: [],
    messages: [
      {
        id: "m",
        text: "Convert 25 degrees",
        attempts: [
          {
            id: "x",
            chat_id: "a",
            requested_model: "local:1",
            actual_model: null,
            status,
            content: "",
            phase: "asking",
            activity,
          },
        ],
      },
    ],
  } as unknown as Chat;
}
const question = {
  kind: "question",
  id: "q1",
  status: "waiting",
  answers: null,
  questions: [
    {
      header: "Scale",
      question: "Which scale is it in?",
      options: [
        { label: "Celsius" },
        { label: "Fahrenheit", description: "°F" },
      ],
    },
  ],
};

it("answers a waiting question with a choice, typed words, or a skip", async () => {
  const onRespond = vi.fn().mockResolvedValue(true);
  const chat = waitingChat([question]);
  render(
    <Transcript
      chat={chat}
      disabled={false}
      onRetry={vi.fn()}
      onRespond={onRespond}
    />,
  );
  expect(screen.getByRole("status").textContent).toContain(
    "Waiting for your answer",
  );
  const send = screen.getByRole("button", { name: "Send answer" });
  expect((send as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText(/Fahrenheit/));
  await act(async () => {
    fireEvent.click(send);
  });
  expect(onRespond).toHaveBeenLastCalledWith(
    chat.messages[0].attempts[0],
    question,
    {
      answers: [{ header: "Scale", selected: ["Fahrenheit"] }],
    },
  );
  fireEvent.change(
    screen.getByLabelText("Your own answer to: Which scale is it in?"),
    { target: { value: "Kelvin, actually" } },
  );
  await act(async () => {
    fireEvent.click(send);
  });
  expect(onRespond).toHaveBeenLastCalledWith(expect.anything(), question, {
    answers: [{ header: "Scale", custom_answer: "Kelvin, actually" }],
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
  });
  expect(onRespond).toHaveBeenLastCalledWith(expect.anything(), question, {
    cancelled: true,
  });
});

it("shows answered questions as a summary without controls", () => {
  render(
    <Transcript
      chat={waitingChat(
        [
          {
            ...question,
            status: "answered",
            answers: { Scale: ["Fahrenheit"] },
          },
        ],
        "complete",
      )}
      disabled={false}
      onRetry={vi.fn()}
      onRespond={vi.fn()}
    />,
  );
  expect(screen.getByText(/→ Fahrenheit/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Send answer" })).toBeNull();
});

it("approves, revises, or postpones a waiting plan", async () => {
  const plan = {
    kind: "plan",
    id: "p1",
    status: "waiting",
    plan: "1. Convert\n2. Check",
    feedback: null,
  };
  const onRespond = vi.fn().mockResolvedValue(true);
  render(
    <Transcript
      chat={waitingChat([plan])}
      disabled={false}
      onRetry={vi.fn()}
      onRespond={onRespond}
    />,
  );
  expect(screen.getByText("Convert")).toBeTruthy();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Approve plan" }));
  });
  expect(onRespond).toHaveBeenLastCalledWith(expect.anything(), plan, {
    decision: "approve",
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
  });
  expect(onRespond).toHaveBeenLastCalledWith(expect.anything(), plan, {
    decision: "dismiss",
  });
  fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
  const changes = screen.getByRole("button", { name: "Send changes" });
  expect((changes as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("Changes to the plan"), {
    target: { value: "Show the formula" },
  });
  await act(async () => {
    fireEvent.click(changes);
  });
  expect(onRespond).toHaveBeenLastCalledWith(expect.anything(), plan, {
    decision: "revise",
    feedback: "Show the formula",
  });
});

it("lists sandbox runs with their code and output", () => {
  render(
    <Transcript
      chat={waitingChat(
        [
          {
            kind: "code",
            id: "c1",
            status: "complete",
            code: "(25 - 32) * 5 / 9",
            output: "-3.888",
            error: null,
          },
          {
            kind: "plan",
            id: "p1",
            status: "approved",
            plan: "1. Convert",
            feedback: null,
          },
        ],
        "complete",
      )}
      disabled={false}
      onRetry={vi.fn()}
    />,
  );
  expect(screen.getByText("Ran Python · Complete")).toBeTruthy();
  expect(screen.getByText("(25 - 32) * 5 / 9")).toBeTruthy();
  expect(screen.getByText("-3.888")).toBeTruthy();
  expect(screen.getByText("Plan · Approved")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Approve plan" })).toBeNull();
});

it("toggles plan mode from the composer", () => {
  const updateChat = vi.fn();
  const app = {
    chat: { id: "a", model: "tools", mode: "normal", attachments: [] },
    draft: "",
    models: [{ name: "tools", available: true, vision: false, tools: true }],
    changeDraft: vi.fn(),
    updateChat,
  } as unknown as ChatController;
  const view = render(<Composer app={app} />);
  const toggle = screen.getByRole("button", { name: "Plan" });
  expect(toggle.getAttribute("aria-pressed")).toBe("false");
  expect(screen.getByText(/Python sandbox/)).toBeTruthy();
  fireEvent.click(toggle);
  expect(updateChat).toHaveBeenCalledWith({ mode: "plan" });
  view.rerender(
    <Composer
      app={{ ...app, chat: { ...app.chat!, mode: "plan" } } as ChatController}
    />,
  );
  expect(screen.getByText(/Plan mode · Vikram proposes a plan/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Plan" }));
  expect(updateChat).toHaveBeenLastCalledWith({ mode: "normal" });
});
