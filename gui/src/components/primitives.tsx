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
