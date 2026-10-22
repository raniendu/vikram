import { useEffect, useLayoutEffect, useRef } from "react";
import { Globe, Hand, Square, Undo2, X } from "lucide-react";
import { desktop, native } from "../bridge";
import type { BrowserControl, BrowserPage } from "../types";

function address(url: string) {
  try {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol)) return "";
    return parsed.host + (parsed.pathname === "/" ? "" : parsed.pathname);
  } catch {
    return "";
  }
}

function status(
  control: BrowserControl,
  phase: string | null,
  running: boolean,
) {
  if (control === "person")
    return "You’re in control. Vikram waits until you hand the browser back.";
  if (phase === "browsing") return "Vikram is using the browser…";
  if (phase === "approving")
    return "Vikram is waiting for your OK in the chat.";
  if (running) return "Vikram is replying.";
  return "Private session · cleared when you close it or quit Vikram.";
}

/**
 * The browser Vikram drives. The page itself is a native WebKit view that the
 * app places over `browser-page`; this component reports where that is.
 */
export function BrowserPanel({
  page,
  control,
  phase,
  running,
  covered,
  onControl,
  onStop,
  onClose,
}: {
  page: BrowserPage;
  control: BrowserControl;
  phase: string | null;
  running: boolean;
  covered: boolean;
  onControl: (control: BrowserControl) => void;
  onStop: () => void;
  onClose: () => void;
}) {
  const view = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const node = view.current;
    if (!desktop || !node) return;
    const report = () => {
      const r = node.getBoundingClientRect();
      void native("browser_bounds", {
        x: r.left,
        y: r.top,
        width: r.width,
        height: r.height,
      }).catch(() => undefined);
    };
    report();
    const observer =
      typeof ResizeObserver === "function" ? new ResizeObserver(report) : null;
    observer?.observe(node);
    addEventListener("resize", report);
    return () => {
      observer?.disconnect();
      removeEventListener("resize", report);
    };
  }, []);
  useEffect(() => {
    // The native page would cover the app's dialogs: hide it under them.
    if (desktop)
      void native("browser_visible", { visible: !covered }).catch(
        () => undefined,
      );
  }, [covered]);
  const where = address(page.url);
  const busy = running && control === "agent";
  return (
    <aside className="browser-pane" aria-label="Vikram’s browser">
      <header className="browser-header">
        <Globe size={15} className="browser-icon" />
        <div className="browser-location">
          <span className="browser-title" title={page.title || undefined}>
            {page.title || where || "Browser"}
          </span>
          {where && page.title && (
            <span className="browser-address" title={page.url}>
              {page.loading && <span className="activity-dot" />}
              {where}
            </span>
          )}
        </div>
        <button
          className="icon-button"
          aria-label="Close the browser"
          title="Close the browser and clear its private session"
          onClick={onClose}
        >
          <X size={16} />
        </button>
      </header>
      <div
        className={`browser-status ${control === "person" ? "in-control" : ""}`}
        role="status"
      >
        <span>{status(control, phase, running)}</span>
        <div className="browser-actions">
          {control === "person" ? (
            <button className="primary" onClick={() => onControl("agent")}>
              <Undo2 size={13} />
              Hand back
            </button>
          ) : (
            <button
              className="secondary"
              title="Use the page yourself; Vikram’s browser actions wait until you hand it back"
              onClick={() => onControl("person")}
            >
              <Hand size={13} />
              Take over
            </button>
          )}
          {running && (
            <button
              className="secondary"
              aria-label="Stop reply"
              title="Stop the reply"
              onClick={onStop}
            >
              <Square size={11} fill="currentColor" />
              Stop
            </button>
          )}
        </div>
      </div>
      <div
        ref={view}
        className={`browser-page ${busy ? "agent" : ""}`}
        data-testid="browser-page"
      >
        <p>{page.loading ? "Loading…" : "The page appears here."}</p>
      </div>
    </aside>
  );
}
