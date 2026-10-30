import { useEffect, useState } from "react";
import { desktop, onRuntime, rpc } from "./bridge";
import type { RuntimeEvent, UpdateStatus } from "./types";

/** Updates: the service checks when the window opens and installs a newer
 * release for the app and CLI in the background; an update from
 * `vikram update` shows up too. The window then offers to restart. */
export function useUpdates() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);

  useEffect(() => {
    if (!desktop) return;
    let disposed = false;
    let off: (() => void) | undefined;
    void onRuntime((event) => {
      if (event.event === "update")
        setStatus(event as RuntimeEvent & UpdateStatus);
    }).then((unlisten) => {
      off = unlisten;
      if (disposed) unlisten();
    });
    void rpc<UpdateStatus>("check_update")
      .then((value) => !disposed && setStatus((old) => old ?? value))
      .catch(() => undefined);
    return () => {
      disposed = true;
      off?.();
    };
  }, []);

  return {
    status,
    /** A restart is waiting and the person has not set it aside. */
    offer:
      status?.state === "ready" && status.version !== dismissed
        ? status.version
        : null,
    dismiss: () => setDismissed(status?.version ?? null),
    check: () =>
      rpc<UpdateStatus>("check_update")
        .then(setStatus)
        .catch(() => undefined),
  };
}
