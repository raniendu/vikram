import type { CSSProperties, ReactNode } from "react";

/** Mono, uppercase, wide-tracked — the site's label voice. */
export function Eyebrow({
  children,
  className,
  style,
}: {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div className={`eyebrow${className ? ` ${className}` : ""}`} style={style}>
      {children}
    </div>
  );
}

/** Hairline separator. The full-ink `.rule` is the heavier sibling. */
export function Rule({ style }: { style?: CSSProperties }) {
  return <div className="hairline" style={style} />;
}

export function Dot({ live }: { live?: boolean }) {
  return <span className={`dot${live ? " live" : ""}`} />;
}

/** Half-filled circle, matching the site's own theme control. */
export function ThemeToggle({ dark, onToggle }: { dark: boolean; onToggle: () => void }) {
  return (
    <button
      className="theme-toggle"
      onClick={onToggle}
      title={dark ? "Switch to light" : "Switch to dark"}
      aria-label="Toggle theme"
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" strokeWidth="1.6" style={{ color: "var(--muted)" }}>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none" />
      </svg>
    </button>
  );
}

/** A stack of labelled fields, one column, at reading width. */
export function Fields({
  children,
  wide,
}: {
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="fields" style={wide ? { maxWidth: "none" } : undefined}>
      {children}
    </div>
  );
}

/** Label, control, hint -- in that source order.
 *
 * `.field-hint` pins itself to column 2, so the control has to come before
 * it or auto-placement drops the control into the 96px label column.
 */
export function Field({
  label,
  hint,
  top,
  children,
}: {
  label: string;
  hint?: ReactNode;
  /** Align the label to the first line of a tall control. */
  top?: boolean;
  children: ReactNode;
}) {
  return (
    <label className="field" style={top ? { alignItems: "start" } : undefined}>
      <span className="eyebrow" style={top ? { paddingTop: 5 } : undefined}>
        {label}
      </span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}
