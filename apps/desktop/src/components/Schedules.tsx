import { useState } from "react";
import {
  ArrowLeft,
  CalendarClock,
  MessageSquarePlus,
  Pause,
  Pencil,
  Play,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import type { ScheduleController } from "../useSchedules";
import type {
  Attempt,
  LocalModel,
  Repeat,
  RunSummary,
  ScheduledTask,
  ScheduleTime,
  TaskDetail,
  TaskFields,
  TaskRun,
  Weekday,
} from "../types";
import { formatModelName } from "../modelNames";
import { Dialog } from "./Dialog";
import { Answer } from "./Transcript";

const DAYS: [Weekday, string][] = [
  ["mon", "Mon"],
  ["tue", "Tue"],
  ["wed", "Wed"],
  ["thu", "Thu"],
  ["fri", "Fri"],
  ["sat", "Sat"],
  ["sun", "Sun"],
];
const RUN_STATUS: Record<string, string> = {
  running: "Running",
  complete: "Complete",
  failed: "Failed",
  stopped: "Stopped",
  interrupted: "Interrupted",
  missed: "Missed",
};
const TRIGGER: Record<string, string> = {
  schedule: "On schedule",
  catch_up: "Caught up after sleep",
  manual: "Run now",
};

export function when(seconds: number | null | undefined) {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function stateText(task: ScheduledTask) {
  if (task.state === "paused") return "Paused";
  if (task.state === "done") return "Done";
  return `Next: ${when(task.next_run)}`;
}

/** A run as one turn of a chat: the prompt, then Vikram's answer. */
function runAttempt(run: TaskRun): Attempt {
  return {
    id: run.id,
    chat_id: run.task_id,
    message_id: run.id,
    request_id: run.id,
    requested_model: run.model ?? "",
    actual_model: run.actual_model,
    thinking: null,
    status: run.status === "missed" ? "interrupted" : run.status,
    content: run.content,
    error: run.error,
    reasoning: run.reasoning,
    activity: run.activity,
    sources: run.sources,
    searches: run.searches,
    phase: "answering",
  };
}

function backgroundNote(background: string): React.ReactNode {
  if (background === "on")
    return "Tasks run on this Mac at their times, even when Vikram is closed. Questions are skipped and the browser is not used.";
  if (background === "unavailable")
    return (
      <>
        Background runs need the installed Vikram app. In development, run{" "}
        <code>uv run vikram schedules --run-due</code>.
      </>
    );
  if (background === "failed")
    return "Vikram could not start its background service. Quit and reopen Vikram, then try again.";
  return null;
}

export function Schedules({
  schedules,
  models,
  defaultModel,
  disabled,
  onOpenChat,
  onDialog,
  sidebarToggle,
}: {
  schedules: ScheduleController;
  models: LocalModel[];
  defaultModel: string | null;
  disabled: boolean;
  onOpenChat: (chatId: string) => void;
  /** A dialog is open: the browser panel must not cover it. */
  onDialog?: (open: boolean) => void;
  sidebarToggle?: React.ReactNode;
}) {
  const [editing, setEditingState] = useState<ScheduledTask | "new" | null>(
    null,
  );
  const [deleting, setDeletingState] = useState<ScheduledTask | null>(null);
  const setEditing = (value: ScheduledTask | "new" | null) => {
    setEditingState(value);
    onDialog?.(value !== null);
  };
  const setDeleting = (value: ScheduledTask | null) => {
    setDeletingState(value);
    onDialog?.(value !== null);
  };
  const { task, run } = schedules;
  const note = backgroundNote(schedules.background);
  return (
    <>
      <header className="conversation-header schedules-header">
        {sidebarToggle}
        {task ? (
          <button
            className="icon-button"
            aria-label={run ? "Back to the task" : "Back to scheduled tasks"}
            onClick={() =>
              void (run ? schedules.openRun(null) : schedules.openTask(null))
            }
          >
            <ArrowLeft size={18} />
          </button>
        ) : (
          <CalendarClock size={18} className="header-icon" />
        )}
        <h1 className="chat-title">
          {run
            ? `${task?.name} · ${when(run.started)}`
            : (task?.name ?? "Scheduled tasks")}
        </h1>
        <div className="model-controls">
          {!task && (
            <button
              className="primary"
              disabled={disabled}
              onClick={() => setEditing("new")}
            >
              <Plus size={15} />
              New task
            </button>
          )}
        </div>
      </header>
      {schedules.error && (
        <div className="notice" role="alert">
          <span>{schedules.error}</span>
          <button
            className="icon-button"
            aria-label="Dismiss notice"
            onClick={() => schedules.setError(null)}
          >
            <X size={15} />
          </button>
        </div>
      )}
      <div className="transcript-wrap">
        <div className="transcript">
          <div className="reading-column">
            {run && task ? (
              <RunView
                run={run}
                disabled={disabled || schedules.working}
                onContinue={async () => {
                  const chat = await schedules.continueInChat(run.id);
                  if (chat) onOpenChat(chat.id);
                }}
              />
            ) : task ? (
              <TaskView
                task={task}
                disabled={disabled || schedules.working}
                onRun={(id) => void schedules.openRun(id)}
                onRunNow={() => void schedules.runNow(task.id)}
                onPause={(paused) => void schedules.setPaused(task.id, paused)}
                onEdit={() => setEditing(task)}
                onDelete={() => setDeleting(task)}
              />
            ) : (
              <>
                {note && <p className="schedules-note">{note}</p>}
                {schedules.tasks.length ? (
                  <ul className="task-list" aria-label="Scheduled tasks">
                    {schedules.tasks.map((item) => (
                      <li key={item.id}>
                        <button
                          className="task-row"
                          onClick={() => void schedules.openTask(item.id)}
                        >
                          <span className="task-name">{item.name}</span>
                          <span className="task-when">{item.description}</span>
                          <span className={`task-state ${item.state}`}>
                            {stateText(item)}
                            {item.last_run &&
                              ` · last ${(RUN_STATUS[item.last_run.status] ?? item.last_run.status).toLowerCase()}`}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="empty-copy schedules-empty">
                    <h2>Nothing scheduled yet.</h2>
                    <p>
                      Ask in any chat, for example “every weekday at 8am,
                      summarize the tech news” or “remind me tomorrow at 3pm to
                      call the dentist”, or create a task here.
                    </p>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      </div>
      {editing && (
        <TaskDialog
          task={editing === "new" ? null : editing}
          models={models}
          defaultModel={defaultModel}
          working={schedules.working}
          onClose={() => setEditing(null)}
          onSave={async (fields) => {
            const saved = await schedules.save(
              fields,
              editing === "new" ? undefined : editing.id,
            );
            if (saved) setEditing(null);
          }}
        />
      )}
      {deleting && (
        <Dialog title="Delete this task?" onClose={() => setDeleting(null)}>
          <p>
            “{deleting.name}” and its run history will be permanently removed.
            Chats you continued from its runs stay.
          </p>
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setDeleting(null)}>
              Cancel
            </button>
            <button
              className="danger-button"
              disabled={schedules.working}
              onClick={async () => {
                if (await schedules.remove(deleting.id)) {
                  setDeleting(null);
                  void schedules.openTask(null);
                }
              }}
            >
              Delete task
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}

function TaskView({
  task,
  disabled,
  onRun,
  onRunNow,
  onPause,
  onEdit,
  onDelete,
}: {
  task: TaskDetail;
  disabled: boolean;
  onRun: (id: string) => void;
  onRunNow: () => void;
  onPause: (paused: boolean) => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const running = task.runs.some((r) => r.status === "running");
  return (
    <section className="task-view" aria-label="Scheduled task">
      <p className="task-when">
        {task.description} · {stateText(task)}
      </p>
      <div className="user-message">
        <div className="user-byline">EACH RUN ASKS</div>
        <p>{task.prompt}</p>
      </div>
      <p className="tool-note">
        {task.model ? formatModelName(task.model) : "No model chosen"}
        {task.origin === "chat"
          ? " · created in a chat"
          : task.origin === "cli"
            ? " · created in the terminal"
            : ""}
      </p>
      <div className="tool-actions task-actions">
        <button
          className="primary"
          disabled={disabled || running || task.run_requested}
          onClick={onRunNow}
        >
          <Play size={13} />
          {task.run_requested ? "Starting…" : "Run now"}
        </button>
        <button
          className="secondary"
          disabled={disabled}
          onClick={() => onPause(task.enabled)}
        >
          {task.enabled ? <Pause size={13} /> : <Play size={13} />}
          {task.enabled ? "Pause" : "Resume"}
        </button>
        <button className="secondary" disabled={disabled} onClick={onEdit}>
          <Pencil size={13} />
          Edit
        </button>
        <button
          className="secondary danger-text"
          disabled={disabled || running}
          onClick={onDelete}
        >
          <Trash2 size={13} />
          Delete
        </button>
      </div>
      <h2 className="runs-heading">Runs</h2>
      {task.runs.length ? (
        <ul className="run-list" aria-label="Run history">
          {task.runs.map((item: RunSummary) => (
            <li key={item.id}>
              <button
                className="run-row"
                disabled={item.status === "missed"}
                onClick={() => onRun(item.id)}
              >
                <span className={`run-status ${item.status}`}>
                  {item.status === "running" && (
                    <span className="activity-dot" />
                  )}
                  {RUN_STATUS[item.status] ?? item.status}
                </span>
                <span>{when(item.started)}</span>
                <span className="run-trigger">{TRIGGER[item.trigger]}</span>
                <span className="run-preview">
                  {item.status === "missed"
                    ? item.error
                    : item.preview || item.error || ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No runs yet.</p>
      )}
    </section>
  );
}

function RunView({
  run,
  disabled,
  onContinue,
}: {
  run: TaskRun;
  disabled: boolean;
  onContinue: () => void;
}) {
  return (
    <article className="turn" aria-label="Scheduled run">
      <div className="user-message">
        <div className="user-byline">
          {(TRIGGER[run.trigger] ?? "Run").toUpperCase()} · {when(run.started)}
        </div>
        <p>{run.prompt}</p>
      </div>
      <Answer attempt={runAttempt(run)} />
      {run.status !== "running" && (
        <div className="tool-actions">
          <button
            className="secondary"
            disabled={disabled}
            onClick={onContinue}
          >
            <MessageSquarePlus size={13} />
            Continue in a chat
          </button>
        </div>
      )}
    </article>
  );
}

function blankSchedule(): ScheduleTime {
  return {
    repeat: "weekly",
    days: ["mon", "tue", "wed", "thu", "fri"],
    time: "08:00",
  };
}

function tomorrow() {
  const day = new Date(Date.now() + 86_400_000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
}

/** Only the fields this kind of schedule uses, as the service expects. */
export function scheduleFields(schedule: ScheduleTime): ScheduleTime {
  const { repeat, time, date, days, day_of_month, every_minutes } = schedule;
  if (repeat === "once") return { repeat, date, time };
  if (repeat === "daily") return { repeat, time };
  if (repeat === "weekly") return { repeat, days, time };
  if (repeat === "monthly") return { repeat, day_of_month, time };
  return { repeat, every_minutes };
}

export function TaskDialog({
  task,
  models,
  defaultModel,
  working,
  onClose,
  onSave,
}: {
  task: ScheduledTask | null;
  models: LocalModel[];
  defaultModel: string | null;
  working: boolean;
  onClose: () => void;
  onSave: (fields: TaskFields) => void;
}) {
  const [name, setName] = useState(task?.name ?? "");
  const [prompt, setPrompt] = useState(task?.prompt ?? "");
  const [schedule, setSchedule] = useState<ScheduleTime>(
    task ? { ...task.schedule } : blankSchedule(),
  );
  const available = models.filter((m) => m.available);
  const [model, setModel] = useState(
    task?.model ?? defaultModel ?? available[0]?.name ?? "",
  );
  const [unit, setUnit] = useState<60 | 1>(
    (task?.schedule.every_minutes ?? 60) % 60 === 0 ? 60 : 1,
  );
  const change = (fields: Partial<ScheduleTime>) =>
    setSchedule((old) => ({ ...old, ...fields }));
  function pick(repeat: Repeat) {
    change({
      repeat,
      time: schedule.time ?? "08:00",
      date: schedule.date ?? tomorrow(),
      days: schedule.days?.length ? schedule.days : ["mon"],
      day_of_month: schedule.day_of_month ?? 1,
      every_minutes: schedule.every_minutes ?? 60,
    });
  }
  const fields = scheduleFields(schedule);
  const valid =
    name.trim() &&
    prompt.trim() &&
    model &&
    (fields.repeat === "interval"
      ? (fields.every_minutes ?? 0) >= 15
      : /^\d{2}:\d{2}$/.test(fields.time ?? "")) &&
    (fields.repeat !== "once" ||
      /^\d{4}-\d{2}-\d{2}$/.test(fields.date ?? "")) &&
    (fields.repeat !== "weekly" || !!fields.days?.length);
  return (
    <Dialog
      title={task ? "Edit scheduled task" : "New scheduled task"}
      onClose={onClose}
    >
      <form
        className="task-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid && !working)
            onSave({
              name: name.trim(),
              prompt: prompt.trim(),
              schedule: fields,
              model,
            });
        }}
      >
        <label className="field-label" htmlFor="task-name">
          Name
        </label>
        <input
          id="task-name"
          maxLength={80}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Morning tech news"
        />
        <label className="field-label" htmlFor="task-prompt">
          What should Vikram do each time?
        </label>
        <textarea
          id="task-prompt"
          rows={4}
          maxLength={4000}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="e.g. Summarize today's top technology news in five bullet points."
        />
        <label className="field-label" htmlFor="task-repeat">
          Repeat
        </label>
        <select
          id="task-repeat"
          value={schedule.repeat}
          onChange={(e) => pick(e.target.value as Repeat)}
        >
          <option value="once">Once</option>
          <option value="daily">Every day</option>
          <option value="weekly">On chosen days</option>
          <option value="monthly">Every month</option>
          <option value="interval">At an interval</option>
        </select>
        {schedule.repeat === "weekly" && (
          <div className="day-picker" role="group" aria-label="Days">
            {DAYS.map(([day, label]) => {
              const on = schedule.days?.includes(day) ?? false;
              return (
                <button
                  type="button"
                  key={day}
                  aria-pressed={on}
                  className={on ? "chosen" : ""}
                  onClick={() =>
                    change({
                      days: on
                        ? (schedule.days ?? []).filter((d) => d !== day)
                        : [...(schedule.days ?? []), day],
                    })
                  }
                >
                  {label}
                </button>
              );
            })}
          </div>
        )}
        <div className="task-time">
          {schedule.repeat === "once" && (
            <div>
              <label className="field-label" htmlFor="task-date">
                Date
              </label>
              <input
                id="task-date"
                type="date"
                value={schedule.date ?? ""}
                onChange={(e) => change({ date: e.target.value })}
              />
            </div>
          )}
          {schedule.repeat === "monthly" && (
            <div>
              <label className="field-label" htmlFor="task-day">
                Day of the month
              </label>
              <input
                id="task-day"
                type="number"
                min={1}
                max={31}
                value={schedule.day_of_month ?? 1}
                onChange={(e) =>
                  change({ day_of_month: Number(e.target.value) })
                }
              />
            </div>
          )}
          {schedule.repeat === "interval" ? (
            <div>
              <label className="field-label" htmlFor="task-every">
                Every
              </label>
              <div className="interval-input">
                <input
                  id="task-every"
                  type="number"
                  min={unit === 60 ? 1 : 15}
                  value={Math.round((schedule.every_minutes ?? 60) / unit)}
                  onChange={(e) =>
                    change({ every_minutes: Number(e.target.value) * unit })
                  }
                />
                <select
                  aria-label="Interval unit"
                  value={unit}
                  onChange={(e) => {
                    const next = Number(e.target.value) as 60 | 1;
                    setUnit(next);
                    change({
                      every_minutes: Math.max(
                        15,
                        Math.round((schedule.every_minutes ?? 60) / unit) *
                          next,
                      ),
                    });
                  }}
                >
                  <option value={1}>minutes</option>
                  <option value={60}>hours</option>
                </select>
              </div>
            </div>
          ) : (
            <div>
              <label className="field-label" htmlFor="task-time">
                Time
              </label>
              <input
                id="task-time"
                type="time"
                value={schedule.time ?? ""}
                onChange={(e) => change({ time: e.target.value.slice(0, 5) })}
              />
            </div>
          )}
        </div>
        <label className="field-label" htmlFor="task-model">
          Model
        </label>
        <select
          id="task-model"
          value={model}
          onChange={(e) => setModel(e.target.value)}
        >
          {!model && (
            <option value="" disabled>
              Select a local model
            </option>
          )}
          {model && !available.some((m) => m.name === model) && (
            <option value={model} disabled>
              {formatModelName(model)} · unavailable
            </option>
          )}
          {available.map((m) => (
            <option key={m.name} value={m.name}>
              {formatModelName(m.name)}
            </option>
          ))}
        </select>
        <p className="tool-note">
          Runs happen on this Mac, even when Vikram is closed. Nobody is there
          to answer questions, so Vikram decides and says what it chose. Web
          search and the Python sandbox work; the browser is not used.
        </p>
        <div className="dialog-actions">
          <button type="button" className="secondary" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={!valid || working}>
            {task ? "Save" : "Create task"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
