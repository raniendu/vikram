// Vikram's in-page browser script. WebKit runs it in Vikram's own script world,
// so the page cannot see, replace, or call these functions; it shares only the
// DOM. It reads the page and performs the one action Vikram asks for. Page
// content is untrusted: nothing here acts on what a page says.
(() => {
  if (globalThis.vikram) return;

  const MAX_ELEMENTS = 80;
  const MAX_NAME = 120;
  const MAX_VALUE = 100;
  const MAX_TEXT = 6000;
  // Lowercase: selectors match SVG elements case-sensitively.
  const SKIP =
    "script,style,noscript,template,head,svg,canvas,iframe,object,embed";
  const CLICKABLE = [
    "a[href]",
    "button",
    "input:not([type=hidden])",
    "select",
    "textarea",
    "summary",
    "[role=button]",
    "[role=link]",
    "[role=checkbox]",
    "[role=radio]",
    "[role=switch]",
    "[role=tab]",
    "[role=menuitem]",
    "[role=menuitemcheckbox]",
    "[role=menuitemradio]",
    "[role=option]",
    "[role=combobox]",
    "[role=textbox]",
    "[role=searchbox]",
    "[role=slider]",
    "[role=spinbutton]",
    "[role=treeitem]",
    "[contenteditable]:not([contenteditable=false])",
    "[onclick]",
    "[tabindex]:not([tabindex='-1'])",
  ].join(",");
  const TEXT_INPUTS = new Set([
    "text",
    "search",
    "email",
    "url",
    "tel",
    "number",
    "password",
    "date",
    "datetime-local",
    "month",
    "week",
    "time",
  ]);
  const SENSITIVE_AUTOCOMPLETE =
    /\b(current-password|new-password|one-time-code|cc-number|cc-csc|cc-exp|cc-exp-month|cc-exp-year|cc-name|cc-given-name|cc-family-name|cc-type)\b/;
  const SENSITIVE_HINT =
    /(pass(word|code|phrase)?\b|\bpin\b|\bcvv\b|\bcvc\b|\bcsc\b|security code|card ?number|credit card|debit card|expir|one[- ]time|\botp\b|verification code|\b2fa\b|two[- ]factor|\bssn\b|social security|\biban\b|routing number|account number)/i;
  const SENSITIVE =
    "Vikram never types into password, payment, or one-time-code fields. Tell the user what is needed so they can enter it themselves.";
  const RISKS = [
    {
      words:
        /\b(buy|purchase|order|checkout|check out|pay|payment|donate|subscribe|upgrade|book|reserve|bid|rent|add funds|top up)\b/i,
      reason: "It looks like it buys or pays for something.",
      links: true,
    },
    {
      words:
        /\b(delete|remove|erase|deactivate|close (my |your )?account|unsubscribe|cancel (my |your |the )?(subscription|order|plan|membership)|transfer|withdraw|send money)\b/i,
      reason:
        "It looks like it deletes or changes something that may not be undone.",
      links: true,
    },
    {
      words:
        /\b(sign ?in|log ?in|log ?on|sign ?up|register|create (an |my |your )?account)\b/i,
      reason: "It looks like it signs in or creates an account.",
      links: false,
    },
    {
      words:
        /\b(post|publish|send|share|tweet|reply|comment|submit|upload|message|invite)\b/i,
      reason: "It looks like it posts, sends, or submits something.",
      links: false,
    },
    {
      words: /\b(confirm|accept|agree|allow|authori[sz]e|grant|consent)\b/i,
      reason: "It looks like it confirms or agrees to something.",
      links: false,
    },
  ];

  const state = { refs: new Map() };

  function clean(text, max) {
    const value = String(text ?? "")
      .replace(/\s+/g, " ")
      .trim();
    return value.length > max ? value.slice(0, max - 1).trimEnd() + "…" : value;
  }

  function textOf(el) {
    const text =
      typeof el.innerText === "string" ? el.innerText : el.textContent;
    return text || "";
  }

  function* roots(root = document) {
    yield root;
    for (const el of root.querySelectorAll("*"))
      if (el.shadowRoot) yield* roots(el.shadowRoot);
  }

  function all(selector) {
    const found = [];
    for (const root of roots()) found.push(...root.querySelectorAll(selector));
    return found;
  }

  function rendered(el) {
    if (typeof el.checkVisibility === "function") {
      if (!el.checkVisibility({ checkVisibilityCSS: true })) return false;
    } else {
      for (
        let node = el;
        node && node.nodeType === 1;
        node = node.parentElement
      ) {
        const style = getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden")
          return false;
      }
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function nearView(rect) {
    return (
      rect.bottom > -0.25 * innerHeight &&
      rect.top < 1.5 * innerHeight &&
      rect.right > 0 &&
      rect.left < innerWidth
    );
  }

  function inputType(el) {
    return el.tagName === "INPUT"
      ? String(el.type || "text").toLowerCase()
      : "";
  }

  function isField(el) {
    if (el.tagName === "TEXTAREA" || el.tagName === "SELECT") return true;
    if (el.tagName === "INPUT") return TEXT_INPUTS.has(inputType(el));
    if (el.isContentEditable) return true;
    return ["textbox", "searchbox", "combobox"].includes(
      el.getAttribute("role"),
    );
  }

  function roleOf(el) {
    const explicit = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
    const role = explicit || implicitRole(el);
    return (
      {
        textbox: "text field",
        searchbox: "search field",
        combobox: "select",
        listbox: "select",
        spinbutton: "number field",
      }[role] || role
    ).slice(0, 40);
  }

  function implicitRole(el) {
    const tag = el.tagName;
    if (tag === "A") return "link";
    if (tag === "BUTTON" || tag === "SUMMARY") return "button";
    if (tag === "SELECT") return "select";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "INPUT") {
      const type = inputType(el);
      if (type === "checkbox" || type === "radio") return type;
      if (["submit", "button", "reset", "image"].includes(type))
        return "button";
      if (type === "search") return "searchbox";
      if (type === "password") return "password field";
      if (type === "range") return "slider";
      if (type === "file") return "file upload";
      if (type === "number") return "spinbutton";
      if (["date", "time", "datetime-local", "month", "week"].includes(type))
        return `${type} field`;
      if (type === "color") return "color picker";
      return "textbox";
    }
    if (el.isContentEditable) return "textbox";
    return "clickable";
  }

  // A label's own words, without the text of the controls inside it (a
  // select's options would otherwise become part of its name).
  function labelText(label) {
    const words = [];
    const walker = document.createTreeWalker(label, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const control =
        node.parentElement &&
        node.parentElement.closest("select,textarea,button,option");
      if (!control || !label.contains(control)) words.push(node.nodeValue);
    }
    return words.join(" ");
  }

  function labelsOf(el) {
    return el.labels ? [...el.labels].map(labelText).join(" ") : "";
  }

  function nameOf(el) {
    const aria = el.getAttribute("aria-label");
    if (aria && aria.trim()) return clean(aria, MAX_NAME);
    const ids = el.getAttribute("aria-labelledby");
    if (ids) {
      const text = ids
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map(textOf)
        .join(" ");
      if (text.trim()) return clean(text, MAX_NAME);
    }
    const labels = labelsOf(el);
    if (labels.trim()) return clean(labels, MAX_NAME);
    const type = inputType(el);
    if (["submit", "button", "reset"].includes(type))
      return clean(el.value || type, MAX_NAME);
    if (type === "image") return clean(el.alt || "image button", MAX_NAME);
    if (!isField(el)) {
      const own = textOf(el);
      if (own.trim()) return clean(own, MAX_NAME);
      const image = el.querySelector && el.querySelector("img[alt]");
      if (image && image.alt.trim()) return clean(image.alt, MAX_NAME);
    }
    return clean(
      el.getAttribute("title") ||
        el.getAttribute("placeholder") ||
        el.getAttribute("name") ||
        "",
      MAX_NAME,
    );
  }

  function sensitive(el) {
    if (inputType(el) === "password") return true;
    if (
      SENSITIVE_AUTOCOMPLETE.test(
        (el.getAttribute("autocomplete") || "").toLowerCase(),
      )
    )
      return true;
    const hints = [
      el.getAttribute("name"),
      el.id,
      el.getAttribute("placeholder"),
      el.getAttribute("aria-label"),
      labelsOf(el),
    ].join(" ");
    return SENSITIVE_HINT.test(hints);
  }

  function valueOf(el) {
    if (!isField(el)) return null;
    if (el.tagName === "SELECT") {
      const option = el.selectedOptions && el.selectedOptions[0];
      return clean(option ? option.text : "", MAX_VALUE);
    }
    const value = el.isContentEditable ? textOf(el) : el.value;
    // Never show what is in a password, payment, or one-time-code field.
    if (sensitive(el)) return value ? "(hidden)" : "";
    return clean(value, MAX_VALUE);
  }

  function statesOf(el) {
    const states = [];
    const role = el.getAttribute("role");
    const type = inputType(el);
    if (el.checked === true || el.getAttribute("aria-checked") === "true")
      states.push("checked");
    else if (
      type === "checkbox" ||
      type === "radio" ||
      ["checkbox", "radio", "switch", "menuitemcheckbox"].includes(role)
    )
      states.push("not checked");
    if (el.disabled || el.getAttribute("aria-disabled") === "true")
      states.push("disabled");
    if (el.getAttribute("aria-expanded") === "true") states.push("expanded");
    else if (el.getAttribute("aria-expanded") === "false")
      states.push("collapsed");
    if (el.getAttribute("aria-selected") === "true") states.push("selected");
    if (
      el.getAttribute("aria-current") &&
      el.getAttribute("aria-current") !== "false"
    )
      states.push("current");
    if (el.required) states.push("required");
    return states.slice(0, 6);
  }

  function describe(el) {
    const name = nameOf(el);
    return clean(`${roleOf(el)}${name ? ` “${name}”` : ""}`, 280);
  }

  function blockOf(el, cache) {
    for (
      let node = el;
      node && node.nodeType === 1;
      node = node.parentElement
    ) {
      if (!cache.has(node)) {
        const display = getComputedStyle(node).display;
        cache.set(node, !/^(inline|contents)/.test(display));
      }
      if (cache.get(node)) return node;
    }
    return document.body;
  }

  // Text from the top of the view downward, in reading order, with line breaks
  // between blocks: what the person sees now, then what scrolling reveals.
  function visibleText() {
    const parts = [];
    let size = 0;
    let last = null;
    const shown = new Map();
    const blocks = new Map();
    for (const root of roots()) {
      const start = root === document ? document.body : root;
      if (!start) continue;
      const walker = document.createTreeWalker(start, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = node.nodeValue.replace(/\s+/g, " ");
        const parent = node.parentElement;
        if (!text.trim() || !parent) continue;
        if (!shown.has(parent)) {
          let visible = !parent.closest(SKIP);
          if (visible) visible = rendered(parent);
          if (visible) visible = parent.getBoundingClientRect().bottom >= 0;
          shown.set(parent, visible);
        }
        if (!shown.get(parent)) continue;
        const block = blockOf(parent, blocks);
        // A new block starts a line; inline text keeps its own spacing.
        const piece =
          block !== last && parts.length ? `\n${text.trimStart()}` : text;
        last = block;
        parts.push(piece);
        size += piece.length;
        if (size > MAX_TEXT) return parts.join("").slice(0, MAX_TEXT);
      }
    }
    return parts
      .join("")
      .replace(/[ \t]*\n[ \t]*/g, "\n")
      .trim();
  }

  function observe() {
    const refs = new Map();
    const listed = new Set();
    const candidates = [];
    for (const el of all(CLICKABLE)) {
      if (!rendered(el)) continue;
      const rect = el.getBoundingClientRect();
      if (!nearView(rect)) continue;
      candidates.push({ el, rect });
    }
    candidates.sort(
      (a, b) =>
        Math.round(a.rect.top / 8) - Math.round(b.rect.top / 8) ||
        a.rect.left - b.rect.left,
    );
    const elements = [];
    for (const { el } of candidates) {
      // A clickable inside another listed one (a span in a button) adds nothing.
      const outer = el.parentElement && el.parentElement.closest(CLICKABLE);
      if (outer && listed.has(outer) && !isField(el)) continue;
      listed.add(el);
      if (elements.length >= MAX_ELEMENTS) continue;
      const ref = elements.length + 1;
      // A strong reference: this world's wrappers for page elements are
      // otherwise collected between calls.
      refs.set(ref, el);
      const item = { ref, role: roleOf(el), name: nameOf(el) };
      const value = valueOf(el);
      if (value !== null) item.value = value;
      const states = statesOf(el);
      if (states.length) item.states = states;
      elements.push(item);
    }
    state.refs = refs;
    const notices = [];
    if (listed.size > elements.length)
      notices.push(
        `Only the first ${elements.length} of ${listed.size} elements in view are numbered.`,
      );
    if (
      [...document.querySelectorAll("iframe")].some((frame) => rendered(frame))
    )
      notices.push(
        "Some content is inside frames that Vikram cannot read or use.",
      );
    const scroller = document.scrollingElement || document.documentElement;
    return {
      url: location.href,
      title: clean(document.title, 300),
      elements,
      text: visibleText(),
      scroll: {
        top: Math.max(0, Math.round(scrollY)),
        height: Math.max(0, Math.round(scroller.scrollHeight)),
        viewport: Math.max(0, Math.round(innerHeight)),
      },
      notices,
    };
  }

  function find(ref) {
    const el = state.refs.get(ref);
    if (!el || !el.isConnected)
      return `Element ${ref} is not on the current page. Use the numbers from the latest page view.`;
    return el;
  }

  function risk(name, link) {
    for (const item of RISKS)
      if ((item.links || !link) && item.words.test(name)) return item.reason;
    return null;
  }

  function submits(el) {
    if (!el.form) return false;
    if (el.tagName === "BUTTON")
      return (el.getAttribute("type") || "submit").toLowerCase() === "submit";
    return ["submit", "image"].includes(inputType(el));
  }

  function signsIn(form) {
    return !!(form && form.querySelector("input[type=password]"));
  }

  function approvalFor(el, action, submit) {
    const name = nameOf(el) || roleOf(el);
    const where = location.hostname || "this page";
    if (action === "type") {
      if (!submit) return null;
      const form = el.form || el.closest("form");
      return {
        summary: clean(`Press Enter in “${name}” on ${where}`, 280),
        reason: signsIn(form)
          ? "It looks like it signs in."
          : form
            ? "It submits a form."
            : "It may submit or send what was typed.",
      };
    }
    const link = roleOf(el) === "link";
    const reason = submits(el)
      ? signsIn(el.form)
        ? "It looks like it signs in."
        : risk(name, false) || "It submits a form."
      : risk(name, link);
    return reason
      ? { summary: clean(`Click “${name}” on ${where}`, 280), reason }
      : null;
  }

  function web(href) {
    try {
      return ["http:", "https:"].includes(
        new URL(href, location.href).protocol,
      );
    } catch {
      return false;
    }
  }

  function press(el) {
    const rect = el.getBoundingClientRect();
    const point = {
      bubbles: true,
      cancelable: true,
      composed: true,
      clientX: rect.left + rect.width / 2,
      clientY: rect.top + rect.height / 2,
      button: 0,
    };
    const pointer = {
      ...point,
      pointerId: 1,
      pointerType: "mouse",
      isPrimary: true,
    };
    const Pointer =
      typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
    el.dispatchEvent(new Pointer("pointerover", pointer));
    el.dispatchEvent(new MouseEvent("mouseover", point));
    el.dispatchEvent(new Pointer("pointerdown", { ...pointer, buttons: 1 }));
    el.dispatchEvent(new MouseEvent("mousedown", { ...point, buttons: 1 }));
    if (typeof el.focus === "function") el.focus({ preventScroll: true });
    el.dispatchEvent(new Pointer("pointerup", pointer));
    el.dispatchEvent(new MouseEvent("mouseup", point));
    // Dispatches the click and runs the element's default action.
    el.click();
  }

  function click(ref, approved) {
    const el = find(ref);
    if (typeof el === "string") return { status: "error", error: el };
    const target = describe(el);
    if (inputType(el) === "file")
      return {
        status: "refused",
        target,
        error:
          "Vikram cannot upload files. Tell the user which file is needed so they can upload it themselves.",
      };
    if (el.disabled || el.getAttribute("aria-disabled") === "true")
      return { status: "error", target, error: `${target} is disabled.` };
    if (!approved) {
      const approval = approvalFor(el, "click", false);
      if (approval)
        return {
          status: "approval",
          target,
          approval,
          url: location.href,
          title: document.title,
        };
    }
    el.scrollIntoView({ block: "center", inline: "nearest" });
    const link = el.closest("a[href]");
    const elsewhere =
      link &&
      link.target &&
      !["_self", "_top", "_parent"].includes(link.target.toLowerCase());
    if (elsewhere && web(link.href)) {
      // Vikram's browser has one page: open new-tab links here instead.
      location.assign(link.href);
      return { status: "done", target };
    }
    press(el);
    return { status: "done", target };
  }

  function nativeSetter(el) {
    const proto =
      el.tagName === "TEXTAREA"
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    return Object.getOwnPropertyDescriptor(proto, "value").set;
  }

  function fill(el, text) {
    if (typeof el.focus === "function") el.focus({ preventScroll: true });
    if (el.tagName === "SELECT") {
      const wanted = text.trim().toLowerCase();
      const options = [...el.options];
      const option =
        options.find((o) => o.text.trim().toLowerCase() === wanted) ||
        options.find((o) => o.value.toLowerCase() === wanted) ||
        options.find((o) => o.text.trim().toLowerCase().includes(wanted));
      if (!option || !wanted)
        return `No option “${clean(text, 60)}” in this select. Options: ${clean(
          options.map((o) => o.text.trim()).join(", "),
          400,
        )}`;
      el.value = option.value;
    } else if (el.isContentEditable) {
      const selection = getSelection();
      selection.selectAllChildren(el);
      if (!document.execCommand("insertText", false, text))
        el.textContent = text;
    } else {
      // The prototype's setter, not the element's: frameworks that track the
      // value (React) then see the change when the input event arrives.
      nativeSetter(el).call(el, text);
      el.dispatchEvent(
        new InputEvent("input", {
          bubbles: true,
          composed: true,
          inputType: "insertText",
          data: text,
        }),
      );
    }
    if (el.tagName === "SELECT")
      el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return null;
  }

  function enter(el) {
    const key = {
      key: "Enter",
      code: "Enter",
      keyCode: 13,
      which: 13,
      bubbles: true,
      cancelable: true,
      composed: true,
    };
    const allowed = el.dispatchEvent(new KeyboardEvent("keydown", key));
    el.dispatchEvent(new KeyboardEvent("keypress", key));
    el.dispatchEvent(new KeyboardEvent("keyup", key));
    const form = el.form || el.closest("form");
    if (allowed && form && el.tagName !== "TEXTAREA") {
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    }
  }

  function type(ref, text, submit, approved) {
    const el = find(ref);
    if (typeof el === "string") return { status: "error", error: el };
    const target = describe(el);
    if (sensitive(el)) return { status: "refused", target, error: SENSITIVE };
    if (!isField(el))
      return {
        status: "error",
        target,
        error: `${target} is not a text field or select; use browser_click for it.`,
      };
    if (el.disabled || el.readOnly)
      return { status: "error", target, error: `${target} cannot be changed.` };
    el.scrollIntoView({ block: "center", inline: "nearest" });
    // Typing first lets the person see what Enter would send before allowing it.
    const error = fill(el, text);
    if (error) return { status: "error", target, error };
    if (submit && !approved) {
      return {
        status: "approval",
        target,
        approval: approvalFor(el, "type", true),
        url: location.href,
        title: document.title,
      };
    }
    if (submit) enter(el);
    return { status: "done", target };
  }

  function scrollables() {
    return all("*").filter((el) => {
      if (el.scrollHeight <= el.clientHeight + 20) return false;
      const overflow = getComputedStyle(el).overflowY;
      return (overflow === "auto" || overflow === "scroll") && rendered(el);
    });
  }

  function scroll(direction) {
    const distance =
      (direction === "up" ? -1 : 1) * Math.round(innerHeight * 0.85);
    const before = scrollY;
    window.scrollBy({ top: distance, behavior: "instant" });
    if (Math.round(scrollY) !== Math.round(before)) return { status: "done" };
    // Pages that scroll an inner panel instead of the window: the largest one.
    const panel = scrollables().sort(
      (a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight,
    )[0];
    if (panel) {
      const top = panel.scrollTop;
      panel.scrollBy({ top: distance, behavior: "instant" });
      if (panel.scrollTop !== top) return { status: "done" };
    }
    return {
      status: "done",
      notice:
        direction === "up"
          ? "Already at the top of the page."
          : "Already at the bottom of the page.",
    };
  }

  function back() {
    history.back();
    return { status: "done" };
  }

  globalThis.vikram = Object.freeze({ observe, click, type, scroll, back });
})();
