import {
  Children,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowDown, Check, ChevronRight, Copy, RotateCcw } from "lucide-react";
import type { Attempt, Chat } from "../types";
import { copyText, openLink } from "../bridge";
import { formatModelName } from "../modelNames";
import { AttachmentView } from "./AttachmentView";

function CopyButton({
  text,
  label = "Copy",
}: {
  text: string;
  label?: string;
}) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  return (
    <button
      className="copy-button"
      title={error ? "Select the text and press Command-C" : label}
      aria-label={label}
      onClick={() => {
        void copyText(text)
          .then(() => {
            setCopied(true);
            setError(false);
            setTimeout(() => setCopied(false), 1600);
          })
          .catch(() => setError(true));
      }}
    >
      {copied ? <Check size={13} /> : <Copy size={13} />}
      <span>{copied ? "Copied" : error ? "Select and ⌘C" : label}</span>
    </button>
  );
}
function CodeBlock({ children }: { children?: React.ReactNode }) {
  const child = Children.toArray(children).find(isValidElement);
  const content =
    child &&
    isValidElement<{ children?: React.ReactNode; className?: string }>(child)
      ? String(child.props.children ?? "").replace(/\n$/, "")
      : "";
  const language =
    child && isValidElement<{ className?: string }>(child)
      ? child.props.className?.replace("language-", "")
      : "";
  return (
    <div className="code-block">
      <div className="code-toolbar">
        <span>{language || "code"}</span>
        <CopyButton text={content} label="Copy code" />
      </div>
      <pre>{children}</pre>
    </div>
  );
}
function ThinkingDisclosure({ attempt }: { attempt: Attempt }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const viewport = useRef<HTMLDivElement>(null);
  const sticky = useRef(true);
  const active = attempt.status === "running" && attempt.phase === "thinking";
  useEffect(() => {
    const node = viewport.current;
    if (open && sticky.current && node) node.scrollTop = node.scrollHeight;
  }, [open, attempt.reasoning]);
  if (!attempt.reasoning) return null;
  return (
    <div className="thinking-disclosure">
      <button
        className="thinking-toggle"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
      >
        <ChevronRight size={13} className={open ? "expanded" : ""} />
        {active && <span className="activity-dot" />}
        <span>{active ? "Thinking…" : "Thinking"}</span>
      </button>
      {open && (
        <div id={id} className="thinking-body">
          <div
            className="thinking-text"
            ref={viewport}
            tabIndex={0}
            aria-label="Model thinking"
            onScroll={() => {
              const node = viewport.current!;
              sticky.current =
                node.scrollHeight - node.scrollTop - node.clientHeight < 40;
            }}
          >
            {attempt.reasoning}
          </div>
          {attempt.reasoning_truncated && (
            <p className="attempt-notice">
              Thinking reached the display limit; the answer is unaffected.
            </p>
          )}
          <CopyButton text={attempt.reasoning} label="Copy thinking" />
        </div>
      )}
    </div>
  );
}
function Answer({ attempt }: { attempt: Attempt }) {
  return (
    <div className={`answer ${attempt.unsaved ? "unsaved" : ""}`}>
      <div className="answer-byline">
        <span className="mini-brand">v.</span>
        <strong>Vikram</strong>
        <span
          className="model-label"
          title={attempt.actual_model ?? attempt.requested_model}
        >
          {formatModelName(attempt.actual_model ?? attempt.requested_model)}
          {!attempt.actual_model && " · requested"}
        </span>
      </div>
      <ThinkingDisclosure attempt={attempt} />
      {attempt.content && (
        <div className="markdown">
          <Markdown
            remarkPlugins={[remarkGfm]}
            skipHtml
            components={{
              pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
              img: ({ alt }) => (
                <span className="blocked-image">
                  [External image blocked{alt ? `: ${alt}` : ""}]
                </span>
              ),
              a: ({ href, children }) => (
                <a
                  href={href}
                  onClick={(e) => {
                    e.preventDefault();
                    if (href && /^https?:\/\//i.test(href))
                      void openLink(href).catch(() => undefined);
                  }}
                >
                  {children}
                </a>
              ),
            }}
          >
            {attempt.content}
          </Markdown>
        </div>
      )}
      {attempt.status === "running" &&
        !(attempt.phase === "thinking" && attempt.reasoning) && (
          <div className="generation-status" role="status">
            <span className="activity-dot" />
            {attempt.phase === "searching"
              ? "Searching the web…"
              : attempt.phase === "fetching"
                ? "Reading sources…"
                : attempt.content
                  ? "Writing…"
                  : attempt.phase === "thinking"
                    ? "Thinking…"
                    : attempt.phase === "loading"
                      ? "Waiting for Ollama…"
                      : "Preparing reply…"}
          </div>
        )}
      {!!attempt.searches?.length && (
        <details className="search-activity">
          <summary>
            Web activity · {attempt.searches.length}{" "}
            {attempt.searches.length === 1 ? "request" : "requests"}
          </summary>
          {attempt.searches.map((search, i) => (
            <div key={i} className="search-record">
              <strong>
                {search.tool === "web_search" ? "Search" : "Read"} ·{" "}
                {search.status}
              </strong>
              <p>
                {(
                  search.arguments.search_queries ??
                  search.arguments.urls ??
                  []
                ).join(" · ")}
              </p>
              {search.error && <p role="status">{search.error}</p>}
            </div>
          ))}
        </details>
      )}
      {attempt.searches?.some((s) => s.status === "failed") && (
        <p className="attempt-notice">
          Web search was unavailable or rate limited. Some current information
          could not be verified.
        </p>
      )}
      {!!attempt.sources?.length && (
        <div className="source-list">
          <span className="source-heading">Retrieved sources</span>
          {attempt.sources.map((source) => (
            <a
              key={source.id}
              href={source.url}
              title={`${source.url} · retrieved ${source.retrieved_at}`}
              onClick={(e) => {
                e.preventDefault();
                void openLink(source.url).catch(() => undefined);
              }}
            >
              <span>{source.id}</span>
              {source.title}
            </a>
          ))}
        </div>
      )}
      {attempt.status !== "running" && attempt.status !== "complete" && (
        <div className="attempt-notice">
          <strong>
            {attempt.status[0].toUpperCase() + attempt.status.slice(1)}
          </strong>{" "}
          · This partial reply is excluded from future context.
          {attempt.error && <p>{attempt.error}</p>}
        </div>
      )}
      {attempt.unsaved && (
        <p className="unsaved-label" role="alert">
          Unsaved reply. Copy this text before closing or reconnecting.
        </p>
      )}
      {attempt.content && (
        <CopyButton text={attempt.content} label="Copy reply" />
      )}
    </div>
  );
}
export function Transcript({
  chat,
  disabled,
  onRetry,
}: {
  chat: Chat;
  disabled: boolean;
  onRetry: () => void;
}) {
  const end = useRef<HTMLDivElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const sticky = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const last = chat.messages.at(-1)?.attempts.at(-1);
  useEffect(() => {
    sticky.current = true;
    setShowJump(false);
    end.current?.scrollIntoView();
  }, [chat.id]);
  useEffect(() => {
    if (sticky.current) end.current?.scrollIntoView();
  }, [last?.content, last?.status, chat.messages.length]);
  return (
    <div className="transcript-wrap">
      <div
        className="transcript"
        ref={viewport}
        onScroll={() => {
          const node = viewport.current!;
          sticky.current =
            node.scrollHeight - node.scrollTop - node.clientHeight < 100;
          setShowJump(!sticky.current);
        }}
      >
        <div className="reading-column">
          {chat.messages.map((message, index) => (
            <section
              className="turn"
              key={message.id}
              aria-label={`Message ${index + 1}`}
            >
              <div className="user-message">
                <div className="user-byline">YOU</div>
                {message.text && <p>{message.text}</p>}
                {chat.attachments.some((a) => a.message_id === message.id) && (
                  <div className="attachments">
                    {chat.attachments
                      .filter((a) => a.message_id === message.id)
                      .map((image) => (
                        <AttachmentView key={image.id} image={image} />
                      ))}
                  </div>
                )}
              </div>
              {message.attempts.map((attempt, i) =>
                i < message.attempts.length - 1 ? (
                  <details className="previous-attempt" key={attempt.id}>
                    <summary>Previous attempt · {attempt.status}</summary>
                    <Answer attempt={attempt} />
                  </details>
                ) : (
                  <Answer key={attempt.id} attempt={attempt} />
                ),
              )}
              {index === chat.messages.length - 1 &&
                message.attempts.length > 0 &&
                !message.attempts.some(
                  (a) => a.status === "running" || a.status === "complete",
                ) && (
                  <button
                    className="retry-button"
                    disabled={disabled}
                    onClick={onRetry}
                  >
                    <RotateCcw size={14} />
                    Retry with selected model
                  </button>
                )}
            </section>
          ))}
          <div ref={end} />
        </div>
      </div>
      {showJump && (
        <button
          className="jump-latest"
          onClick={() => {
            sticky.current = true;
            end.current?.scrollIntoView({ behavior: "smooth" });
          }}
        >
          <ArrowDown size={14} />
          Latest
        </button>
      )}
    </div>
  );
}
