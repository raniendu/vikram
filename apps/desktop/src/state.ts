import type { Chat, RuntimeEvent } from "./types";

export function applyAttempt(chat: Chat, event: RuntimeEvent): Chat {
  if (event.chat_id !== chat.id || !event.attempt_id || !event.request_id)
    return chat;
  return {
    ...chat,
    messages: chat.messages.map((m) => ({
      ...m,
      attempts: m.attempts.map((a) => {
        if (a.id !== event.attempt_id || a.request_id !== event.request_id)
          return a;
        // A terminal state never accepts a delayed running event.
        if (a.status !== "running" && event.status === "running") return a;
        if ((event.revision ?? 0) < (a.revision ?? 0)) return a;
        return {
          ...a,
          content: event.content ?? a.content,
          reasoning: event.reasoning ?? a.reasoning,
          reasoning_truncated:
            event.reasoning_truncated ?? a.reasoning_truncated,
          actual_model: event.actual_model ?? a.actual_model,
          status: event.status ?? a.status,
          error: event.error ?? a.error,
          unsaved: event.unsaved ?? a.unsaved,
          phase: event.phase ?? a.phase,
          sources: event.sources ?? a.sources,
          searches: event.searches ?? a.searches,
          revision: event.revision ?? a.revision,
        };
      }),
    })),
  };
}

export function reconcileActive(
  active: import("./types").Active | null,
  events: Map<string, RuntimeEvent>,
) {
  if (!active) return null;
  const event = events.get(active.attempt_id);
  return event?.request_id === active.request_id &&
    event.status &&
    event.status !== "running"
    ? null
    : active;
}

export function recoverChat(
  chat: Chat,
  events: Map<string, RuntimeEvent>,
): Chat {
  return {
    ...chat,
    messages: chat.messages.map((m) => ({
      ...m,
      attempts: m.attempts.map((a) => {
        const event = events.get(a.id);
        // Tool calls can replace prior text. Only checkpoint order establishes freshness.
        const newerUnsaved =
          event?.request_id === a.request_id &&
          event.unsaved &&
          (event.revision ?? 0) > (a.revision ?? 0);
        return newerUnsaved
          ? {
              ...a,
              content: event.content ?? a.content,
              reasoning: event.reasoning ?? a.reasoning,
              reasoning_truncated:
                event.reasoning_truncated ?? a.reasoning_truncated,
              actual_model: event.actual_model ?? a.actual_model,
              sources: event.sources ?? a.sources,
              searches: event.searches ?? a.searches,
              revision: event.revision,
              status: "interrupted" as const,
              unsaved: true,
              error:
                event.error ??
                "Unsaved text recovered from this window. Copy it before closing.",
            }
          : a;
      }),
    })),
  };
}

export interface DraftRecord {
  text: string;
  revision: number;
}
export interface Submission extends DraftRecord {
  requestId: string;
}
export function recoverDraft(
  chat: Chat,
  draft: DraftRecord,
  submission?: Submission,
): DraftRecord {
  const committed =
    submission &&
    chat.messages.some((m) =>
      m.attempts.some((a) => a.request_id === submission.requestId),
    );
  return {
    text:
      committed &&
      draft.text === submission.text &&
      draft.revision === submission.revision
        ? chat.draft
        : draft.text,
    revision: Math.max(chat.draft_revision, draft.revision) + 1,
  };
}
