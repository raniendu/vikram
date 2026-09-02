/**
 * Reads a session's Server-Sent Events stream.
 *
 * `EventSource` cannot send an Authorization header, so this parses the SSE
 * framing off a plain `fetch` body instead. The framing is small: blank-line
 * separated blocks of `event:` / `data:` / `id:` lines.
 */
import { connect } from "./client";

export type EventType =
  | "session.ready"
  | "session.closed"
  | "turn.started"
  | "text.delta"
  | "thinking.delta"
  | "tool.call"
  | "tool.result"
  | "approval.requested"
  | "approval.resolved"
  | "turn.finished"
  | "turn.failed"
  | "turn.cancelled"
  | "heartbeat";

export interface StreamEvent {
  type: EventType;
  seq: number;
  session_id: string;
  turn_id: string | null;
  column_id: string | null;
  ts: number;
  payload: Record<string, any>;
}

export interface StreamHandle {
  close: () => void;
}

export function streamSession(
  sessionId: string,
  onEvent: (event: StreamEvent) => void,
  onError?: (error: Error) => void,
): StreamHandle {
  const controller = new AbortController();

  (async () => {
    const { base_url, token } = await connect();
    const response = await fetch(
      `${base_url}/v1/sessions/${sessionId}/events`,
      {
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
      },
    );
    if (!response.body) throw new Error("No event stream body.");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });

      let split: number;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        // ": heartbeat" comments keep the connection open; nothing to parse.
        const dataLine = block
          .split("\n")
          .find((line) => line.startsWith("data: "));
        if (!dataLine) continue;
        try {
          onEvent(JSON.parse(dataLine.slice(6)) as StreamEvent);
        } catch {
          /* a partial frame; the next chunk completes it */
        }
      }
    }
  })().catch((error) => {
    if (controller.signal.aborted) return;
    onError?.(error as Error);
  });

  return { close: () => controller.abort() };
}
