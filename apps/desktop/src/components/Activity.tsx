import { useId, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Globe,
  ListChecks,
  MessageCircleQuestionMark,
  ShieldAlert,
  SquareTerminal,
  Undo2,
} from "lucide-react";
import type {
  ActivityRecord,
  Attempt,
  BrowserRecord,
  CodeRecord,
  PlanRecord,
  QuestionRecord,
  Reply,
} from "../types";
import { CopyButton } from "./CopyButton";

export type Respond = (
  attempt: Attempt,
  record: ActivityRecord,
  reply: Reply,
) => Promise<boolean>;

const QUESTION_STATUS: Record<string, string> = {
  declined: "Skipped",
  timed_out: "Not answered in time",
};
const PLAN_STATUS: Record<string, string> = {
  approved: "Approved",
  changes_requested: "Changes requested",
  not_approved: "Not approved",
  timed_out: "Not reviewed in time",
};
const CODE_STATUS: Record<string, string> = {
  complete: "Complete",
  failed: "Failed",
};
const BROWSER_DONE: Record<string, string> = {
  open: "Opened",
  click: "Clicked",
  type: "Typed into",
  scroll: "Scrolled",
  back: "Went back",
  look: "Looked at the page",
};
const BROWSER_STATUS: Record<string, string> = {
  denied: "not allowed",
  refused: "refused",
  failed: "failed",
  timed_out: "timed out",
  stopped: "stopped",
  interrupted: "interrupted",
  waiting: "waiting for your OK",
  paused: "waiting for the browser",
};

function host(url: string | null) {
  try {
    return url ? new URL(url).host : "";
  } catch {
    return "";
  }
}

const BROWSER_DOING: Record<string, string> = {
  open: "Opening",
  click: "Clicking",
  type: "Typing into",
  scroll: "Scrolling",
  back: "Going back",
  look: "Looking at the page",
};
const PENDING = new Set(["running", "waiting", "paused"]);

export function browserStep(record: BrowserRecord) {
  const verbs = PENDING.has(record.status) ? BROWSER_DOING : BROWSER_DONE;
  let text = verbs[record.action] ?? "Used the browser";
  if (record.target) text += ` ${record.target}`;
  if (record.text) text += `: “${record.text}”`;
  return text;
}

function useSending(onReply: (reply: Reply) => Promise<boolean>) {
  const [sending, setSending] = useState(false);
  async function submit(reply: Reply) {
    setSending(true);
    try {
      await onReply(reply);
    } finally {
      setSending(false);
    }
  }
  return [sending, submit] as const;
}

function QuestionCard({
  record,
  live,
  onReply,
}: {
  record: QuestionRecord;
  live: boolean;
  onReply: (reply: Reply) => Promise<boolean>;
}) {
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const [typed, setTyped] = useState<Record<string, string>>({});
  const [sending, submit] = useSending(onReply);
  const id = useId();
  if (!live)
    return (
      <div className="tool-card" role="group" aria-label="Vikram's question">
        {record.questions.map((q) => {
          const answer = record.answers?.[q.header];
          return (
            <p key={q.header} className="tool-summary">
              <MessageCircleQuestionMark size={13} />
              <span>
                {q.question}{" "}
                <strong>
                  →{" "}
                  {answer?.length
                    ? answer.join(", ")
                    : (QUESTION_STATUS[record.status] ?? "Not answered")}
                </strong>
              </span>
            </p>
          );
        })}
      </div>
    );
  const ready = record.questions.every(
    (q) => typed[q.header]?.trim() || picked[q.header]?.length,
  );
  return (
    <form
      className="tool-card waiting"
      aria-label="Vikram's question"
      onSubmit={(e) => {
        e.preventDefault();
        if (!ready || sending) return;
        void submit({
          answers: record.questions.map((q) =>
            typed[q.header]?.trim()
              ? { header: q.header, custom_answer: typed[q.header].trim() }
              : { header: q.header, selected: picked[q.header] },
          ),
        });
      }}
    >
      <div className="tool-heading">
        <MessageCircleQuestionMark size={14} />
        Vikram needs your answer
      </div>
      {record.questions.map((q, index) => (
        <fieldset key={q.header} className="question">
          <legend>{q.question}</legend>
          {q.options.map((option) => {
            const chosen = picked[q.header]?.includes(option.label) ?? false;
            return (
              <label
                key={option.label}
                className={`option ${chosen ? "chosen" : ""}`}
              >
                <input
                  type={q.multi_select ? "checkbox" : "radio"}
                  name={`${id}-${index}`}
                  checked={chosen}
                  disabled={sending}
                  onChange={() => {
                    setPicked((old) => {
                      const current = old[q.header] ?? [];
                      const next = !q.multi_select
                        ? [option.label]
                        : chosen
                          ? current.filter((label) => label !== option.label)
                          : [...current, option.label];
                      return { ...old, [q.header]: next };
                    });
                    setTyped((old) => ({ ...old, [q.header]: "" }));
                  }}
                />
                <span>
                  {option.label}
                  {option.description && <small>{option.description}</small>}
                </span>
              </label>
            );
          })}
          <input
            className="own-answer"
            aria-label={`Your own answer to: ${q.question}`}
            placeholder="Or type your own answer"
            maxLength={2000}
            disabled={sending}
            value={typed[q.header] ?? ""}
            onChange={(e) => {
              const value = e.target.value;
              setTyped((old) => ({ ...old, [q.header]: value }));
              if (value) setPicked((old) => ({ ...old, [q.header]: [] }));
            }}
          />
        </fieldset>
      ))}
      <div className="tool-actions">
        <button type="submit" className="primary" disabled={!ready || sending}>
          Send answer
        </button>
        <button
          type="button"
          className="secondary"
          disabled={sending}
          onClick={() => void submit({ cancelled: true })}
        >
          Skip
        </button>
      </div>
    </form>
  );
}

function PlanCard({
  record,
  live,
  onReply,
}: {
  record: PlanRecord;
  live: boolean;
  onReply: (reply: Reply) => Promise<boolean>;
}) {
  const [changing, setChanging] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [sending, submit] = useSending(onReply);
  const plan = (
    <div className="markdown plan-body">
      <Markdown remarkPlugins={[remarkGfm]} skipHtml>
        {record.plan}
      </Markdown>
    </div>
  );
  if (!live)
    return (
      <details className="tool-run">
        <summary>
          <ListChecks size={13} />
          Plan · {PLAN_STATUS[record.status] ?? "Not reviewed"}
        </summary>
        {plan}
        {record.feedback && (
          <p className="tool-note">Your changes: {record.feedback}</p>
        )}
      </details>
    );
  return (
    <section className="tool-card waiting" aria-label="Plan for your approval">
      <div className="tool-heading">
        <ListChecks size={14} />
        Plan for your approval
      </div>
      {plan}
      {changing ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (feedback.trim() && !sending)
              void submit({ decision: "revise", feedback: feedback.trim() });
          }}
        >
          <textarea
            className="own-answer"
            aria-label="Changes to the plan"
            placeholder="What should change?"
            rows={3}
            maxLength={2000}
            autoFocus
            disabled={sending}
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
          />
          <div className="tool-actions">
            <button
              type="submit"
              className="primary"
              disabled={!feedback.trim() || sending}
            >
              Send changes
            </button>
            <button
              type="button"
              className="secondary"
              disabled={sending}
              onClick={() => setChanging(false)}
            >
              Back
            </button>
          </div>
        </form>
      ) : (
        <div className="tool-actions">
          <button
            className="primary"
            disabled={sending}
            onClick={() => void submit({ decision: "approve" })}
          >
            Approve plan
          </button>
          <button
            className="secondary"
            disabled={sending}
            onClick={() => setChanging(true)}
          >
            Request changes
          </button>
          <button
            className="secondary"
            disabled={sending}
            onClick={() => void submit({ decision: "dismiss" })}
          >
            Not now
          </button>
        </div>
      )}
    </section>
  );
}

function CodeRun({ record }: { record: CodeRecord }) {
  const running = record.status === "running";
  const result = record.error ?? record.output;
  return (
    <details className="tool-run">
      <summary>
        {running ? (
          <span className="activity-dot" />
        ) : (
          <SquareTerminal size={13} />
        )}
        {running
          ? "Running Python in the sandbox…"
          : `Ran Python · ${CODE_STATUS[record.status] ?? "Stopped"}`}
      </summary>
      <div className="code-block">
        <div className="code-toolbar">
          <span>python · sandbox</span>
          <CopyButton text={record.code} label="Copy code" />
        </div>
        <pre>
          <code>{record.code}</code>
        </pre>
      </div>
      {!running && (
        <div className="code-block">
          <div className="code-toolbar">
            <span>{record.error ? "error" : "output"}</span>
          </div>
          <pre>{result || "(no output)"}</pre>
        </div>
      )}
    </details>
  );
}

function BrowserApproval({
  record,
  onReply,
}: {
  record: BrowserRecord;
  onReply: (reply: Reply) => Promise<boolean>;
}) {
  const [sending, submit] = useSending(onReply);
  return (
    <section
      className="tool-card waiting"
      aria-label="Browser action for your approval"
    >
      <div className="tool-heading">
        <ShieldAlert size={14} />
        Allow this browser action?
      </div>
      <p className="approval-summary">
        {record.approval?.summary ?? browserStep(record)}
      </p>
      <p className="tool-note">
        {record.approval?.reason}
        {host(record.url) && ` · ${host(record.url)}`}
      </p>
      <div className="tool-actions">
        <button
          className="primary"
          disabled={sending}
          onClick={() => void submit({ decision: "approve" })}
        >
          Allow
        </button>
        <button
          className="secondary"
          disabled={sending}
          onClick={() => void submit({ decision: "dismiss" })}
        >
          Don’t allow
        </button>
      </div>
    </section>
  );
}

function BrowserPaused({ onHandBack }: { onHandBack?: () => void }) {
  return (
    <section className="tool-card waiting" aria-label="Browser in your control">
      <div className="tool-heading">
        <Globe size={14} />
        Vikram is waiting for the browser
      </div>
      <p className="tool-note">
        You took the browser over. Vikram continues when you hand it back.
      </p>
      {onHandBack && (
        <div className="tool-actions">
          <button className="primary" onClick={onHandBack}>
            <Undo2 size={13} />
            Hand back
          </button>
        </div>
      )}
    </section>
  );
}

function BrowserLog({
  records,
  running,
}: {
  records: BrowserRecord[];
  running: boolean;
}) {
  const last = records[records.length - 1];
  const where = host(
    [...records].reverse().find((record) => record.url)?.url ?? null,
  );
  return (
    <details className="tool-run browser-log" open={running || undefined}>
      <summary>
        {running && last.status === "running" ? (
          <span className="activity-dot" />
        ) : (
          <Globe size={13} />
        )}
        Browser · {records.length} {records.length === 1 ? "action" : "actions"}
        {where && ` · ${where}`}
      </summary>
      <ol className="browser-steps">
        {records.map((record) => (
          <li key={record.id} className={`browser-step ${record.status}`}>
            <span>{browserStep(record)}</span>
            {record.status !== "complete" && record.status !== "running" && (
              <strong>
                {" "}
                · {BROWSER_STATUS[record.status] ?? record.status}
              </strong>
            )}
            {record.error && <small>{record.error}</small>}
          </li>
        ))}
      </ol>
    </details>
  );
}

export function Activity({
  attempt,
  onRespond,
  onHandBack,
}: {
  attempt: Attempt;
  onRespond?: Respond;
  onHandBack?: () => void;
}) {
  if (!attempt.activity?.length) return null;
  const running = attempt.status === "running" && !attempt.unsaved;
  // Consecutive browser actions share one log; waiting ones stand out as cards.
  const groups: (ActivityRecord | BrowserRecord[])[] = [];
  for (const record of attempt.activity) {
    const previous = groups[groups.length - 1];
    if (record.kind === "browser" && Array.isArray(previous))
      previous.push(record);
    else groups.push(record.kind === "browser" ? [record] : record);
  }
  return (
    <div className="tool-activity">
      {groups.map((group) => {
        if (!Array.isArray(group)) return renderRecord(group);
        const waiting = group.find(
          (record) =>
            running &&
            (record.status === "waiting" || record.status === "paused"),
        );
        return (
          <div key={`browser-${group[0].id}`} className="browser-group">
            <BrowserLog records={group} running={running} />
            {waiting?.status === "waiting" && onRespond && (
              <BrowserApproval
                record={waiting}
                onReply={(reply) => onRespond(attempt, waiting, reply)}
              />
            )}
            {waiting?.status === "paused" && (
              <BrowserPaused onHandBack={onHandBack} />
            )}
          </div>
        );
      })}
    </div>
  );

  function renderRecord(record: ActivityRecord) {
    const live =
      !!onRespond &&
      attempt.status === "running" &&
      !attempt.unsaved &&
      record.status === "waiting";
    const onReply = (reply: Reply) =>
      onRespond ? onRespond(attempt, record, reply) : Promise.resolve(false);
    const key = `${record.kind}-${record.id}`;
    if (record.kind === "question")
      return (
        <QuestionCard key={key} record={record} live={live} onReply={onReply} />
      );
    if (record.kind === "plan")
      return (
        <PlanCard key={key} record={record} live={live} onReply={onReply} />
      );
    if (record.kind === "code") return <CodeRun key={key} record={record} />;
    return null;
  }
}
