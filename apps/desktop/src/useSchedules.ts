import { useEffect, useRef, useState } from "react";
import { desktop, onRuntime, rpc } from "./bridge";
import type {
  Background,
  Chat,
  ScheduledTask,
  TaskDetail,
  TaskFields,
  TaskRun,
} from "./types";

/** Scheduled tasks, one open task and run, and the actions the window offers.
 *
 * Runs happen in the background (even with the app closed); the service tells
 * the window when tasks or runs change, and this hook reloads what is open.
 */
export function useSchedules() {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [background, setBackground] = useState<Background>("unavailable");
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [run, setRun] = useState<TaskRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const open = useRef<{ task: string | null; run: string | null }>({
    task: null,
    run: null,
  });
  const fail = (e: unknown) =>
    setError(
      typeof e === "string" ? e : "This action could not finish. Try again.",
    );

  async function refresh() {
    const list = await rpc<{ tasks: ScheduledTask[]; background: Background }>(
      "schedules",
    );
    setTasks(list.tasks);
    setBackground(list.background);
    const { task: taskId, run: runId } = open.current;
    if (taskId) {
      if (list.tasks.some((t) => t.id === taskId))
        setTask(await rpc<TaskDetail>("schedule", { task_id: taskId }));
      else {
        open.current = { task: null, run: null };
        setTask(null);
        setRun(null);
      }
    }
    if (runId && open.current.run === runId) {
      try {
        setRun(await rpc<TaskRun>("schedule_run", { run_id: runId }));
      } catch {
        open.current.run = null;
        setRun(null);
      }
    }
  }

  useEffect(() => {
    if (!desktop) return;
    let disposed = false;
    let off: (() => void) | undefined;
    let pending = false;
    void onRuntime((event) => {
      if (event.event !== "schedules" || pending) return;
      // Several changes arrive together while a run streams: reload once.
      pending = true;
      setTimeout(() => {
        pending = false;
        if (!disposed) void refresh().catch(() => undefined);
      }, 150);
    }).then((unlisten) => {
      off = unlisten;
      if (disposed) unlisten();
    });
    void refresh().catch(() => undefined);
    return () => {
      disposed = true;
      off?.();
    };
  }, []);

  async function act<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setWorking(true);
    setError(null);
    try {
      const result = await fn();
      await refresh();
      return result;
    } catch (e) {
      fail(e);
      return undefined;
    } finally {
      setWorking(false);
    }
  }

  return {
    tasks,
    background,
    task,
    run,
    error,
    setError,
    working,
    refresh: () => refresh().catch(fail),
    async openTask(id: string | null) {
      open.current = { task: id, run: null };
      setRun(null);
      if (!id) return setTask(null);
      try {
        setTask(await rpc<TaskDetail>("schedule", { task_id: id }));
      } catch (e) {
        fail(e);
      }
    },
    async openRun(id: string | null) {
      open.current.run = id;
      if (!id) return setRun(null);
      try {
        setRun(await rpc<TaskRun>("schedule_run", { run_id: id }));
      } catch (e) {
        fail(e);
      }
    },
    save: (fields: TaskFields, id?: string) =>
      act(() =>
        id
          ? rpc<TaskDetail>("update_schedule", { task_id: id, fields })
          : rpc<TaskDetail>("create_schedule", { fields }),
      ),
    setPaused: (id: string, paused: boolean) =>
      act(() =>
        rpc<TaskDetail>("update_schedule", {
          task_id: id,
          fields: { enabled: !paused },
        }),
      ),
    runNow: (id: string) => act(() => rpc("run_schedule", { task_id: id })),
    async remove(id: string) {
      const done = await act(() => rpc("delete_schedule", { task_id: id }));
      return done !== undefined;
    },
    /** Copy a finished run into a new chat, so the person can follow up. */
    continueInChat: (runId: string) =>
      act(() => rpc<Chat>("open_run", { run_id: runId })),
  };
}
export type ScheduleController = ReturnType<typeof useSchedules>;
