// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const bridge = vi.hoisted(() => ({ native: vi.fn() }));
vi.mock("../bridge", () => ({
  desktop: true,
  native: bridge.native,
  copyText: vi.fn(),
  openLink: vi.fn(),
  rpc: vi.fn(),
}));
import { BrowserPanel } from "./BrowserPanel";
import { Activity } from "./Activity";
import type { Attempt, BrowserRecord } from "../types";

beforeEach(() => {
  bridge.native.mockReset();
  bridge.native.mockResolvedValue(null);
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

const page = {
  open: true,
  url: "https://shop.example/kettles",
  title: "Kettles",
  loading: false,
};

function panel(overrides: Partial<Parameters<typeof BrowserPanel>[0]> = {}) {
  const handlers = { onControl: vi.fn(), onStop: vi.fn(), onClose: vi.fn() };
  const view = render(
    <BrowserPanel
      page={page}
      control="agent"
      phase={null}
      running={false}
      covered={false}
      {...handlers}
      {...overrides}
    />,
  );
  return { ...handlers, view };
}

it("shows the page and places the native view over its area", () => {
  panel();
  expect(screen.getByText("Kettles")).toBeTruthy();
  expect(screen.getByText("shop.example/kettles")).toBeTruthy();
  expect(screen.getByText(/Private session/)).toBeTruthy();
  const bounds = bridge.native.mock.calls.find(([c]) => c === "browser_bounds");
  expect(bounds?.[1]).toEqual(
    expect.objectContaining({
      x: expect.any(Number),
      width: expect.any(Number),
    }),
  );
  expect(bridge.native).toHaveBeenCalledWith("browser_visible", {
    visible: true,
  });
});

it("hides the native view under the app's dialogs", () => {
  const { view, onControl, onStop, onClose } = panel();
  view.rerender(
    <BrowserPanel
      page={page}
      control="agent"
      phase={null}
      running={false}
      covered
      onControl={onControl}
      onStop={onStop}
      onClose={onClose}
    />,
  );
  expect(bridge.native).toHaveBeenLastCalledWith("browser_visible", {
    visible: false,
  });
});

it("lets the person take over, hand back, stop, and close", () => {
  const first = panel({ running: true, phase: "browsing" });
  expect(screen.getByText("Vikram is using the browser…")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /Take over/ }));
  expect(first.onControl).toHaveBeenCalledWith("person");
  fireEvent.click(screen.getByRole("button", { name: "Stop reply" }));
  expect(first.onStop).toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Close the browser" }));
  expect(first.onClose).toHaveBeenCalled();
  cleanup();
  const second = panel({ control: "person", running: true, phase: "paused" });
  expect(screen.getByText(/You’re in control/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Take over/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /Hand back/ }));
  expect(second.onControl).toHaveBeenCalledWith("agent");
});

function record(overrides: Partial<BrowserRecord>): BrowserRecord {
  return {
    kind: "browser",
    id: "b1",
    action: "click",
    status: "complete",
    target: "link “Kettles”",
    text: null,
    url: "https://shop.example/",
    title: "Shop",
    error: null,
    approval: null,
    ...overrides,
  };
}

function attempt(records: BrowserRecord[], status = "running"): Attempt {
  return {
    id: "a",
    chat_id: "c",
    message_id: "m",
    request_id: "r",
    requested_model: "local:1",
    actual_model: null,
    thinking: null,
    status,
    content: "",
    error: null,
    activity: records,
  } as Attempt;
}

it("asks the person to allow a consequential action in the chat", () => {
  const waiting = record({
    id: "b2",
    target: "button “Place order”",
    status: "waiting",
    approval: {
      summary: "Click “Place order” on shop.example",
      reason: "It looks like it buys or pays for something.",
    },
  });
  const onRespond = vi.fn().mockResolvedValue(true);
  const current = attempt([
    record({ action: "open", target: "https://shop.example/" }),
    waiting,
  ]);
  render(<Activity attempt={current} onRespond={onRespond} />);
  expect(screen.getByText("Click “Place order” on shop.example")).toBeTruthy();
  expect(screen.getByText(/buys or pays/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Allow" }));
  expect(onRespond).toHaveBeenCalledWith(current, waiting, {
    decision: "approve",
  });
  cleanup();
  render(<Activity attempt={current} onRespond={onRespond} />);
  fireEvent.click(screen.getByRole("button", { name: "Don’t allow" }));
  expect(onRespond).toHaveBeenLastCalledWith(current, waiting, {
    decision: "dismiss",
  });
});

it("offers to hand the browser back while Vikram waits for it", () => {
  const onHandBack = vi.fn();
  render(
    <Activity
      attempt={attempt([
        record({ status: "paused", action: "look", target: null }),
      ])}
      onRespond={vi.fn()}
      onHandBack={onHandBack}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: /Hand back/ }));
  expect(onHandBack).toHaveBeenCalled();
});

it("logs finished browser actions without live controls", () => {
  render(
    <Activity
      attempt={attempt(
        [
          record({ action: "open", target: "https://shop.example/" }),
          record({
            id: "b2",
            action: "type",
            target: "search field “Search”",
            text: "copper kettle",
          }),
          record({
            id: "b3",
            status: "denied",
            target: "button “Place order”",
          }),
          record({
            id: "b4",
            action: "type",
            status: "refused",
            target: "password field “Password”",
            error: "Vikram never types into password fields.",
          }),
          record({ id: "b5", status: "waiting", target: "button “Pay”" }),
        ],
        "stopped",
      )}
      onRespond={vi.fn()}
    />,
  );
  expect(screen.getByText(/Browser · 5 actions · shop.example/)).toBeTruthy();
  expect(screen.getByText("Opened https://shop.example/")).toBeTruthy();
  expect(
    screen.getByText("Typed into search field “Search”: “copper kettle”"),
  ).toBeTruthy();
  expect(screen.getByText("· not allowed")).toBeTruthy();
  expect(
    screen.getByText("Vikram never types into password fields."),
  ).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
});
