import { describe, expect, it } from "vitest";
import { applyAttempt } from "./state";
import type { Chat, RuntimeEvent } from "./types";
const chat = {
  id: "a",
  messages: [
    {
      id: "m",
      text: "hello",
      created: 1,
      attempts: [
        { id: "x", request_id: "r", status: "stopped", content: "partial" },
      ],
    },
  ],
} as Chat;
const late = {
  v: 1,
  event: "attempt",
  chat_id: "a",
  attempt_id: "x",
  request_id: "r",
  status: "running",
  content: "late",
} as RuntimeEvent;
describe("attempt routing", () => {
  it("rejects chunks after stop", () =>
    expect(applyAttempt(chat, late).messages[0].attempts[0].content).toBe(
      "partial",
    ));
  it("rejects chunks for another chat", () =>
    expect(applyAttempt(chat, { ...late, chat_id: "b" })).toBe(chat));
  it("rejects a request mismatch", () =>
    expect(
      applyAttempt(chat, { ...late, request_id: "old" }).messages[0].attempts[0]
        .content,
    ).toBe("partial"));
  it("keeps unsaved output available to copy", () => {
    const active = {
      ...chat,
      messages: [
        {
          ...chat.messages[0],
          attempts: [
            { ...chat.messages[0].attempts[0], status: "running" as const },
          ],
        },
      ],
    };
    expect(
      applyAttempt(active, { ...late, status: "interrupted", unsaved: true })
        .messages[0].attempts[0],
    ).toMatchObject({ content: "late", unsaved: true });
  });
});

import { reconcileActive, recoverChat, recoverDraft } from "./state";
describe("recovery", () => {
  it("does not resurrect a completed generation from an old snapshot", () => {
    expect(
      reconcileActive(
        { chat_id: "a", attempt_id: "x", request_id: "r" },
        new Map([["x", { ...late, status: "complete" }]]),
      ),
    ).toBeNull();
  });
  it("prefers newer persisted text over an older interrupted overlay", () => {
    const saved = {
      ...chat,
      messages: [
        {
          ...chat.messages[0],
          attempts: [
            {
              ...chat.messages[0].attempts[0],
              status: "interrupted" as const,
              content: "hello world",
              revision: 3,
            },
          ],
        },
      ],
    };
    expect(
      recoverChat(
        saved,
        new Map([
          [
            "x",
            {
              ...late,
              status: "interrupted",
              content: "hello",
              unsaved: true,
              revision: 2,
            },
          ],
        ]),
      ).messages[0].attempts[0].content,
    ).toBe("hello world");
  });
  it("preserves genuinely unsaved newer output", () => {
    expect(
      recoverChat(
        chat,
        new Map([
          [
            "x",
            {
              ...late,
              status: "interrupted",
              content: "partial with unsaved tail",
              reasoning: "Unsaved thinking",
              reasoning_truncated: true,
              revision: 1,
              unsaved: true,
            },
          ],
        ]),
      ).messages[0].attempts[0],
    ).toMatchObject({
      content: "partial with unsaved tail",
      unsaved: true,
      reasoning: "Unsaved thinking",
      reasoning_truncated: true,
    });
  });
  it("does not recreate a draft whose send acknowledgement was lost", () => {
    expect(
      recoverDraft(
        { ...chat, draft: "", draft_revision: 4 },
        { text: "hello", revision: 3 },
        { text: "hello", revision: 3, requestId: "r" },
      ),
    ).toEqual({ text: "", revision: 5 });
  });
  it("recovers a shorter final answer and its evidence after a tool call", () => {
    const sources = [
      {
        id: "S1",
        title: "Docs",
        url: "https://example.org",
        excerpt: "42",
        retrieved_at: "now",
      },
    ];
    const searches = [
      {
        tool: "web_search",
        arguments: { search_queries: ["docs"] },
        status: "complete",
        error: null,
      },
    ];
    const saved = {
      ...chat,
      messages: [
        {
          ...chat.messages[0],
          attempts: [
            {
              ...chat.messages[0].attempts[0],
              content: "Checking the web now…",
              revision: 1,
            },
          ],
        },
      ],
    };
    expect(
      recoverChat(
        saved,
        new Map([
          [
            "x",
            {
              ...late,
              status: "interrupted",
              content: "42",
              revision: 2,
              unsaved: true,
              sources,
              searches,
            },
          ],
        ]),
      ).messages[0].attempts[0],
    ).toMatchObject({
      content: "42",
      sources,
      searches,
      revision: 2,
      unsaved: true,
    });
  });
  it("preserves a newer draft typed after that send", () => {
    expect(
      recoverDraft(
        { ...chat, draft: "", draft_revision: 4 },
        { text: "another thought", revision: 5 },
        { text: "hello", revision: 3, requestId: "r" },
      ).text,
    ).toBe("another thought");
  });
  it("preserves an edited draft even when its text matches the sent message", () => {
    expect(
      recoverDraft(
        { ...chat, draft: "", draft_revision: 4 },
        { text: "hello", revision: 5 },
        { text: "hello", revision: 3, requestId: "r" },
      ).text,
    ).toBe("hello");
  });
});

describe("web source routing", () => {
  it("keeps source evidence on the matching running attempt only", () => {
    const active = {
      ...chat,
      messages: [
        {
          ...chat.messages[0],
          attempts: [
            { ...chat.messages[0].attempts[0], status: "running" as const },
          ],
        },
      ],
    };
    const sources = [
      {
        id: "S1",
        title: "Docs",
        url: "https://example.org",
        excerpt: "Evidence",
        retrieved_at: "now",
      },
    ];
    const searches = [
      {
        tool: "web_search",
        arguments: { search_queries: ["official docs"] },
        status: "complete",
        error: null,
      },
    ];
    const event = { ...late, sources, searches };
    expect(applyAttempt(active, event).messages[0].attempts[0]).toMatchObject({
      sources,
      searches,
    });
    expect(
      applyAttempt(chat, event).messages[0].attempts[0],
    ).not.toHaveProperty("sources");
    expect(applyAttempt(active, { ...event, chat_id: "elsewhere" })).toBe(
      active,
    );
  });
});
