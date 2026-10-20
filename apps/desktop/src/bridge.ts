import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { RuntimeEvent } from "./types";

export const desktop = isTauri();
export const rpc = <T>(method: string, params: Record<string, unknown> = {}) =>
  invoke<T>("rpc", { method, params });
export const native = <T>(
  command: string,
  params: Record<string, unknown> = {},
) => invoke<T>(command, params);
export const onRuntime = (fn: (event: RuntimeEvent) => void) =>
  listen<RuntimeEvent>("runtime", (e) => fn(e.payload));
export const onNative = (event: string, fn: () => void) => listen(event, fn);
export const copyText = async (text: string) =>
  desktop ? native("copy_text", { text }) : navigator.clipboard.writeText(text);
export const openLink = (url: string) => native("open_link", { url });
