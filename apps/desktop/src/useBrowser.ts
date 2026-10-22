import { useEffect, useState } from "react";
import { desktop, native, onBrowser } from "./bridge";
import type { BrowserPage } from "./types";

const CLOSED: BrowserPage = { open: false, url: "", title: "", loading: false };

/** The app's browser panel: its current page, and closing it. */
export function useBrowser() {
  const [page, setPage] = useState<BrowserPage>(CLOSED);
  useEffect(() => {
    if (!desktop) return;
    let live = true;
    let off: (() => void) | undefined;
    void onBrowser((next) => {
      if (live) setPage(next);
    }).then((unlisten) => {
      if (live) off = unlisten;
      else unlisten();
    });
    void native<BrowserPage>("browser_state")
      .then((state) => {
        if (live) setPage(state);
      })
      .catch(() => undefined);
    return () => {
      live = false;
      off?.();
    };
  }, []);
  async function close() {
    await native("browser_close");
  }
  return { page, close };
}
export type BrowserController = ReturnType<typeof useBrowser>;
