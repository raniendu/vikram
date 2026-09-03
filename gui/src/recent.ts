/**
 * Workspaces you have opened before, most recent first.
 *
 * Sessions are in-memory in the API and die with it, so the list of folders
 * you actually work in has to be kept somewhere else. It is per-user and
 * per-machine, and losing it costs one trip through the folder picker — which
 * is exactly what localStorage is for.
 */

const KEY = "vikram.recentWorkspaces";
const LIMIT = 6;

export function recentWorkspaces(): string[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((entry): entry is string => typeof entry === "string");
  } catch {
    // Private window, blocked site data, or something hand-edited into the
    // key. An empty list is always a correct answer here.
    return [];
  }
}

export function rememberWorkspace(path: string): string[] {
  const next = [path, ...recentWorkspaces().filter((p) => p !== path)].slice(
    0,
    LIMIT,
  );
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* the choice just will not persist */
  }
  return next;
}

/** Trailing path segment, for a label that fits a narrow row. */
export function basename(path: string): string {
  const parts = path.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || path;
}

/** `/Users/me/code/vikram` → `~/code/vikram`, so the row reads at a glance. */
export function tildify(path: string, home: string | null): string {
  if (home && path.startsWith(home)) return `~${path.slice(home.length)}`;
  return path;
}
