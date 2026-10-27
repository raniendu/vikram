// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Activity } from "./Activity";
import { Schedules, TaskDialog, scheduleFields } from "./Schedules";
import type {
  Attempt,
  LocalModel,
  RuntimeEvent,
  ScheduledTask,
  TaskDetail,
  TaskRun,
} from "../types";
import type { ScheduleController } from "../useSchedules";

const bridge = vi.hoisted(() => ({
  rpc: vi.fn(),
  listeners: [] as ((event: RuntimeEvent) => void)[],
}));
vi.mock("../bridge", () => ({
  desktop: true,
  copyText: vi.fn(),
  openLink: vi.fn(),
  rpc: bridge.rpc,
  onRuntime: (fn: (event: RuntimeEvent) => void) => {
    bridge.listeners.push(fn);
    return Promise.resolve(() => undefined);
  },
}));
import { useSchedules } from "../useSchedules";

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  bridge.rpc.mockReset();
  bridge.listeners.length = 0;
});
afterEach(cleanup);

const MODEL = { name: "local:1", available: true } as LocalModel;
const TASK = {
  id: "t".repeat(32),
  name: "Morning news",
  prompt: "Summarize the news.",
  schedule: {
    repeat: "weekly",
    days: ["mon", "tue", "wed", "thu", "fri"],
    time: "08:00",
  },
  description: "Weekdays at 08:00",
  model: "local:1",
  thinking: null,
  enabled: true,
  origin: "chat",
  next_run: 1_800_000_000,
  run_requested: false,
  state: "scheduled",
  created: 1,
  updated: 1,
  last_run: null,
} as ScheduledTask;
const RUN = {
  id: "r".repeat(32),
  task_id: TASK.id,
  trigger: "schedule",
  scheduled_for: 1_800_000_000,
  started: 1_800_000_000,
  finished: 1_800_000_060,
  status: "complete",
  error: null,
  model: "local:1",
  actual_model: "local:1",
  prompt: "Summarize the news.",
  content: "Today: **three** stories.",
  reasoning: "",
  activity: [],
  sources: [],
  searches: [],
} as TaskRun;

function controller(fields: Partial<ScheduleController> = {}) {
  return {
    tasks: [TASK],
    background: "on",
    task: null,
    run: null,
    error: null,
    setError: vi.fn(),
    working: false,
    refresh: vi.fn(),
    openTask: vi.fn(),
    openRun: vi.fn(),
    save: vi.fn().mockResolvedValue(TASK),
    setPaused: vi.fn(),
    runNow: vi.fn(),
    remove: vi.fn().mockResolvedValue(true),
    continueInChat: vi.fn().mockResolvedValue({ id: "c".repeat(32) }),
    ...fields,
  } as unknown as ScheduleController;
}

function view(schedules: ScheduleController, onOpenChat = vi.fn()) {
  return render(
    <Schedules
      schedules={schedules}
      models={[MODEL]}
      defaultModel="local:1"
      disabled={false}
      onOpenChat={onOpenChat}
    />,
  );
}

it("lists tasks with their schedule and next run, and opens one", () => {
  const schedules = controller();
  view(schedules);
  expect(screen.getByText(/even when Vikram is closed/)).toBeTruthy();
  const row = screen.getByRole("button", { name: /Morning news/ });
  expect(within(row).getByText("Weekdays at 08:00")).toBeTruthy();
  expect(within(row).getByText(/^Next:/)).toBeTruthy();
  fireEvent.click(row);
  expect(schedules.openTask).toHaveBeenCalledWith(TASK.id);
});

it("explains how to start when nothing is scheduled, and in development", () => {
  view(controller({ tasks: [], background: "unavailable" }));
  expect(screen.getByText("Nothing scheduled yet.")).toBeTruthy();
  expect(screen.getByText(/need the installed Vikram app/)).toBeTruthy();
});

it("runs, pauses, deletes, and shows the run history of a task", async () => {
  const detail = {
    ...TASK,
    runs: [
      { ...RUN, preview: "Today: three stories." },
      {
        ...RUN,
        id: "m".repeat(32),
        status: "missed",
        error: "The Mac was asleep or off at the scheduled time.",
      },
    ],
  } as TaskDetail;
  const schedules = controller({ task: detail });
  view(schedules);
  fireEvent.click(screen.getByRole("button", { name: /Run now/ }));
  expect(schedules.runNow).toHaveBeenCalledWith(TASK.id);
  fireEvent.click(screen.getByRole("button", { name: /Pause/ }));
  expect(schedules.setPaused).toHaveBeenCalledWith(TASK.id, true);
  const history = screen.getByRole("list", { name: "Run history" });
  const missed = within(history).getByRole("button", { name: /Missed/ });
  expect((missed as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(within(history).getByRole("button", { name: /Complete/ }));
  expect(schedules.openRun).toHaveBeenCalledWith(RUN.id);
  fireEvent.click(screen.getByRole("button", { name: /Delete/ }));
  const dialog = screen.getByRole("dialog", { name: "Delete this task?" });
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete task" }));
  await waitFor(() => expect(schedules.remove).toHaveBeenCalledWith(TASK.id));
});

it("shows a run as a turn and continues it in a chat", async () => {
  const onOpenChat = vi.fn();
  const schedules = controller({ task: { ...TASK, runs: [RUN] }, run: RUN });
  view(schedules, onOpenChat);
  expect(screen.getByText("Summarize the news.")).toBeTruthy();
  expect(screen.getByText("three").tagName).toBe("STRONG");
  fireEvent.click(screen.getByRole("button", { name: /Continue in a chat/ }));
  await waitFor(() => expect(onOpenChat).toHaveBeenCalledWith("c".repeat(32)));
  expect(schedules.continueInChat).toHaveBeenCalledWith(RUN.id);
});

it("creates a task with only the fields its schedule uses", () => {
  const onSave = vi.fn();
  render(
    <TaskDialog
      task={null}
      models={[MODEL]}
      defaultModel="local:1"
      working={false}
      onClose={vi.fn()}
      onSave={onSave}
    />,
  );
  const create = screen.getByRole("button", { name: "Create task" });
  expect((create as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("Name"), {
    target: { value: "Gym" },
  });
  fireEvent.change(screen.getByLabelText(/What should Vikram do/), {
    target: { value: "Remind me to go to the gym." },
  });
  // Weekdays by default; drop Friday, add Saturday.
  fireEvent.click(screen.getByRole("button", { name: "Fri" }));
  fireEvent.click(screen.getByRole("button", { name: "Sat" }));
  fireEvent.change(screen.getByLabelText("Time"), {
    target: { value: "18:30" },
  });
  fireEvent.click(create);
  expect(onSave).toHaveBeenCalledWith({
    name: "Gym",
    prompt: "Remind me to go to the gym.",
    model: "local:1",
    schedule: {
      repeat: "weekly",
      days: ["mon", "tue", "wed", "thu", "sat"],
      time: "18:30",
    },
  });
  expect(
    scheduleFields({
      repeat: "interval",
      every_minutes: 90,
      time: "08:00",
      days: ["mon"],
    }),
  ).toEqual({ repeat: "interval", every_minutes: 90 });
});

it("asks before saving a task the model created, then links to it", async () => {
  const record = {
    kind: "schedule",
    id: "call",
    action: "create",
    status: "waiting",
    task_id: null,
    name: "Morning news",
    description: "Weekdays at 08:00",
    prompt: "Summarize the news.",
    next_run: null,
    error: null,
  } as const;
  const attempt = {
    id: "a",
    status: "running",
    activity: [record],
  } as unknown as Attempt;
  const onRespond = vi.fn().mockResolvedValue(true);
  const onOpenTask = vi.fn();
  const { rerender } = render(
    <Activity attempt={attempt} onRespond={onRespond} />,
  );
  const card = screen.getByRole("region", {
    name: "Scheduled task for your approval",
  });
  expect(
    within(card).getByText("Morning news · Weekdays at 08:00"),
  ).toBeTruthy();
  expect(within(card).getByText("Summarize the news.")).toBeTruthy();
  fireEvent.click(within(card).getByRole("button", { name: "Save task" }));
  await waitFor(() =>
    expect(onRespond).toHaveBeenCalledWith(attempt, record, {
      decision: "approve",
    }),
  );
  rerender(
    <Activity
      attempt={
        {
          ...attempt,
          status: "complete",
          activity: [{ ...record, status: "complete", task_id: TASK.id }],
        } as unknown as Attempt
      }
      onOpenTask={onOpenTask}
    />,
  );
  expect(screen.getByText(/Scheduled “Morning news” · Weekdays/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Open" }));
  expect(onOpenTask).toHaveBeenCalledWith(TASK.id);
});

it("reloads the open task when a background run changes it", async () => {
  const detail = { ...TASK, runs: [] } as TaskDetail;
  bridge.rpc.mockImplementation(async (method: string) =>
    method === "schedules"
      ? { tasks: [TASK], background: "on" }
      : method === "schedule"
        ? detail
        : null,
  );
  const { result } = renderHook(() => useSchedules());
  await waitFor(() => expect(result.current.tasks).toHaveLength(1));
  expect(result.current.background).toBe("on");
  await act(() => result.current.openTask(TASK.id));
  expect(result.current.task?.runs).toEqual([]);
  detail.runs = [RUN];
  vi.useFakeTimers();
  try {
    act(() =>
      bridge.listeners.forEach((fn) => fn({ v: 1, event: "schedules" })),
    );
    await act(async () => {
      vi.advanceTimersByTime(200);
    });
  } finally {
    vi.useRealTimers();
  }
  await waitFor(() => expect(result.current.task?.runs).toHaveLength(1));
  // A task deleted elsewhere closes.
  bridge.rpc.mockImplementation(async (method: string) =>
    method === "schedules" ? { tasks: [], background: "off" } : null,
  );
  await act(() => result.current.refresh());
  expect(result.current.task).toBeNull();
});
