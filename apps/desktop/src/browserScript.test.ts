// @vitest-environment jsdom
// The page script WebKit runs in Vikram's own script world, here run in jsdom
// with a stand-in layout: what it lists, refuses, and asks approval for.
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SOURCE from "../src-tauri/src/browser.js?raw";

interface Element {
  ref: number;
  role: string;
  name: string;
  value?: string;
  states?: string[];
}
interface Vikram {
  observe(): {
    url: string;
    title: string;
    elements: Element[];
    text: string;
    notices: string[];
  };
  click(ref: number, approved: boolean): Record<string, unknown>;
  type(
    ref: number,
    text: string,
    submit: boolean,
    approved: boolean,
  ): Record<string, unknown>;
}

function load(html: string): Vikram {
  document.title = "Shop";
  document.body.innerHTML = html;
  // jsdom has no layout: each element sits at its data-top, 20px tall.
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      const top = Number(
        this.closest<HTMLElement>("[data-top]")?.dataset.top ?? 10,
      );
      return {
        top,
        bottom: top + 20,
        left: 10,
        right: 110,
        width: 100,
        height: 20,
        x: 10,
        y: top,
        toJSON() {},
      };
    },
  );
  delete (globalThis as { vikram?: Vikram }).vikram;
  new Function(SOURCE)();
  return (globalThis as unknown as { vikram: Vikram }).vikram;
}

function ref(view: ReturnType<Vikram["observe"]>, name: string) {
  const element = view.elements.find((e) => e.name === name);
  if (!element) throw new Error(`no ${name}: ${JSON.stringify(view.elements)}`);
  return element.ref;
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => {
  vi.restoreAllMocks();
});

it("numbers what can be used, in layout order, and hides sensitive values", () => {
  const vikram = load(`
    <a href="/kettles" data-top="300">Kettles</a>
    <label data-top="100">Search <input id="q" value="copper"></label>
    <button data-top="100">Go</button>
    <label data-top="200">Password <input type="password" value="hunter2"></label>
    <label data-top="220">Size <select><option>Small</option><option selected>Large</option></select></label>
    <label data-top="240"><input type="checkbox" checked required> Gift wrap</label>
    <button style="display:none">Hidden</button>
    <p data-top="400">Free shipping <b>today</b> only.</p>
    <script>ignored()</script>
  `);
  const view = vikram.observe();
  expect(view.title).toBe("Shop");
  expect(view.elements.map((e) => e.name)).toEqual([
    "Search",
    "Go",
    "Password",
    "Size",
    "Gift wrap",
    "Kettles",
  ]);
  expect(view.elements[0]).toMatchObject({
    ref: 1,
    role: "text field",
    value: "copper",
  });
  expect(view.elements[1]).toMatchObject({ role: "button" });
  expect(view.elements[2]).toMatchObject({
    role: "password field",
    value: "(hidden)",
  });
  expect(view.elements[3]).toMatchObject({ role: "select", value: "Large" });
  expect(view.elements[4]).toMatchObject({
    role: "checkbox",
    states: ["checked", "required"],
  });
  expect(view.elements[5]).toMatchObject({ role: "link" });
  expect(view.text).toContain("Free shipping today only.");
  expect(view.text).not.toContain("ignored");
  expect(view.text).not.toContain("hunter2");
});

it("asks before submitting, signing in, buying, posting, or agreeing", () => {
  const vikram = load(`
    <form><input name="q" aria-label="Query"><button>Search</button></form>
    <form><input name="user" aria-label="User"><input type="password" aria-label="Pass">
      <button type="submit">Continue</button></form>
    <button type="button">Buy now</button>
    <button type="button">Post reply</button>
    <a href="/account/delete">Delete account</a>
    <a href="/docs">Docs</a>
    <a href="/login">Log in</a>
    <label><input type="checkbox"> I agree to the terms</label>
    <button type="button">Next page</button>
  `);
  const view = vikram.observe();
  const reason = (name: string) =>
    (
      vikram.click(ref(view, name), false).approval as
        { reason: string } | undefined
    )?.reason;
  expect(reason("Search")).toBe("It submits a form.");
  expect(reason("Continue")).toBe("It looks like it signs in.");
  expect(reason("Buy now")).toMatch(/buys or pays/);
  expect(reason("Post reply")).toMatch(/posts, sends/);
  expect(reason("Delete account")).toMatch(/deletes/);
  expect(reason("I agree to the terms")).toMatch(/agrees/);
  // Following a link or paging is not consequential.
  expect(vikram.click(ref(view, "Next page"), false)).toMatchObject({
    status: "done",
  });
  expect(reason("Docs")).toBeUndefined();
  expect(reason("Log in")).toBeUndefined();
  const approval = vikram.click(ref(view, "Buy now"), false);
  expect(approval).toMatchObject({
    status: "approval",
    target: "button “Buy now”",
    approval: { summary: "Click “Buy now” on localhost" },
  });
});

it("clicks only what was approved, and only elements still on the page", () => {
  const vikram = load(
    `<button type="button">Buy now</button><button>Gone</button>`,
  );
  let bought = 0;
  document.querySelector("button")!.addEventListener("click", () => bought++);
  const view = vikram.observe();
  expect(vikram.click(ref(view, "Buy now"), false).status).toBe("approval");
  expect(bought).toBe(0);
  expect(vikram.click(ref(view, "Buy now"), true)).toMatchObject({
    status: "done",
  });
  expect(bought).toBe(1);
  const gone = ref(view, "Gone");
  document.querySelectorAll("button")[1].remove();
  expect(vikram.click(gone, true)).toMatchObject({
    status: "error",
    error: expect.stringContaining("not on the current page"),
  });
  expect(vikram.click(99, true).status).toBe("error");
});

it("never types passwords, payment details, or codes, and never uploads", () => {
  const vikram = load(`
    <label>Password <input type="password"></label>
    <label>Card number <input name="cc"></label>
    <input aria-label="Code" autocomplete="one-time-code">
    <label>Upload <input type="file"></label>
    <label>Name <input></label>
  `);
  const view = vikram.observe();
  for (const name of ["Password", "Card number", "Code"]) {
    const result = vikram.type(ref(view, name), "secret", false, true);
    expect(result).toMatchObject({ status: "refused" });
    expect(result.error).toMatch(/never types/);
  }
  expect(document.querySelector<HTMLInputElement>("input")!.value).toBe("");
  expect(vikram.click(ref(view, "Upload"), true)).toMatchObject({
    status: "refused",
  });
  expect(vikram.type(ref(view, "Name"), "Asha", false, false)).toMatchObject({
    status: "done",
    target: "text field “Name”",
  });
});

it("fills fields the way frameworks notice, and Enter waits for approval", () => {
  const vikram = load(`
    <form><label>Search <input id="q"></label></form>
    <label>Size <select><option value="s">Small</option><option value="l">Large</option></select></label>
  `);
  const input = document.querySelector<HTMLInputElement>("#q")!;
  const seen: string[] = [];
  for (const type of ["input", "change", "keydown"])
    input.addEventListener(type, () => seen.push(type));
  let submitted = 0;
  document.querySelector("form")!.addEventListener("submit", (e) => {
    e.preventDefault();
    submitted++;
  });
  const view = vikram.observe();
  const search = ref(view, "Search");
  const pending = vikram.type(search, "copper kettle", true, false);
  // Typed so the person can see it, but not sent until they allow Enter.
  expect(input.value).toBe("copper kettle");
  expect(pending).toMatchObject({
    status: "approval",
    approval: { reason: "It submits a form." },
  });
  expect(submitted).toBe(0);
  expect(vikram.type(search, "copper kettle", true, true).status).toBe("done");
  expect(submitted).toBe(1);
  expect(seen).toEqual(["input", "change", "input", "change", "keydown"]);
  const size = ref(view, "Size");
  expect(vikram.type(size, "large", false, false).status).toBe("done");
  expect(document.querySelector("select")!.value).toBe("l");
  expect(vikram.type(size, "Huge", false, false)).toMatchObject({
    status: "error",
    error: expect.stringContaining("Options: Small, Large"),
  });
});
