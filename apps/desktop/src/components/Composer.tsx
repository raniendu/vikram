import { useEffect, useRef, useState } from "react";
import { ArrowUp, ListChecks, Paperclip, Square } from "lucide-react";
import type { ChatController } from "../useChats";
import { AttachmentView } from "./AttachmentView";

export function Composer({ app }: { app: ChatController }) {
  const input = useRef<HTMLTextAreaElement>(null);
  const [composing, setComposing] = useState(false);
  const chat = app.chat!;
  const model = app.models.find((m) => m.name === chat.model && m.available);
  const images = chat.attachments.filter((a) => a.message_id === null);
  const invalidImages =
    images.some((a) => a.kind !== "document") && !model?.vision;
  const planning = chat.mode === "plan";
  const busyElsewhere = app.active && app.active.chat_id !== chat.id;
  const runningHere = app.active?.chat_id === chat.id;
  const canSend =
    !!model &&
    !app.offline &&
    !app.sending &&
    !app.importing &&
    !app.active &&
    !invalidImages &&
    !composing &&
    (!!app.draft.trim() || !!images.length);
  useEffect(() => {
    input.current?.focus();
  }, [chat.id]);
  useEffect(() => {
    if (input.current) {
      input.current.style.height = "auto";
      input.current.style.height =
        Math.min(input.current.scrollHeight, 200) + "px";
    }
  }, [app.draft]);
  return (
    <div className="composer-region">
      <div className="reading-column">
        {invalidImages && (
          <p className="composer-warning">
            Choose an image-capable model or remove the draft images to send.
          </p>
        )}
        {planning && (
          <p className="plan-hint">
            Plan mode · Vikram proposes a plan for your approval before it runs
            code or does the work.
          </p>
        )}
        {busyElsewhere && (
          <p className="composer-warning">
            A reply is running in “
            {app.chats.find((c) => c.id === app.active?.chat_id)?.title ??
              "another chat"}
            ”. Your draft stays here.
          </p>
        )}
        <div className={`composer ${app.importing ? "importing" : ""}`}>
          {images.length > 0 && (
            <div className="attachments draft-attachments">
              {images.map((image) => (
                <AttachmentView
                  key={image.id}
                  image={image}
                  onRemove={
                    app.sending || app.importing
                      ? undefined
                      : () => void app.removeAttachment(image.id)
                  }
                />
              ))}
            </div>
          )}
          <textarea
            ref={input}
            id="message-input"
            aria-label="Message"
            placeholder={
              planning ? "Describe what you want planned…" : "Write a message…"
            }
            value={app.draft}
            rows={1}
            disabled={app.sending}
            onChange={(e) => app.changeDraft(e.target.value)}
            onCompositionStart={() => setComposing(true)}
            onCompositionEnd={() => setComposing(false)}
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing &&
                !composing &&
                e.keyCode !== 229
              ) {
                e.preventDefault();
                if (canSend) void app.send();
              }
            }}
            onPaste={(e) => {
              const images = Array.from(e.clipboardData.files);
              if (images.length) {
                e.preventDefault();
                void app.importAttachments(images);
              }
            }}
          />
          <div className="composer-toolbar">
            <button
              className="icon-button"
              title="Attach images, PDF, Word, text/code, CSV, or Excel"
              aria-label="Attach files"
              disabled={app.offline || app.sending || app.importing}
              onClick={() => void app.importAttachments("pick_attachments")}
            >
              <Paperclip size={18} />
            </button>
            <button
              className={`mode-toggle ${planning ? "on" : ""}`}
              aria-pressed={planning}
              title={
                planning
                  ? "Plan mode is on: approve a plan before Vikram works"
                  : "Plan mode: approve a plan before Vikram works"
              }
              disabled={app.offline}
              onClick={() =>
                void app.updateChat({ mode: planning ? "normal" : "plan" })
              }
            >
              <ListChecks size={15} />
              Plan
            </button>
            <span className="composer-hint">
              {app.importing
                ? "Importing files…"
                : "Shift + Enter for a new line"}
            </span>
            {runningHere ? (
              <button
                className="send-button stop-button"
                aria-label="Stop reply"
                onClick={() => void app.stop()}
              >
                <Square size={14} fill="currentColor" />
              </button>
            ) : (
              <button
                className="send-button"
                aria-label="Send message"
                title={
                  busyElsewhere
                    ? "Wait for the active reply to finish"
                    : "Send message"
                }
                disabled={!canSend}
                onClick={() => void app.send()}
              >
                {app.sending ? (
                  <span className="spinner" />
                ) : (
                  <ArrowUp size={18} />
                )}
              </button>
            )}
          </div>
        </div>
        <div className="composer-footnote">
          <span>
            Local model · Web {model?.tools ? "available" : "unavailable"}
            {model?.tools ? " · Python sandbox" : ""}
          </span>
          <span>
            {model?.context_length
              ? `${model.context_length.toLocaleString()} token context allocated`
              : "Saved history · finite model context"}
          </span>
        </div>
      </div>
    </div>
  );
}
