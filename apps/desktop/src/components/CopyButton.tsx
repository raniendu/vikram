import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { copyText } from "../bridge";

export function CopyButton({
  text,
  label = "Copy",
}: {
  text: string;
  label?: string;
}) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  return (
    <button
      className="copy-button"
      title={error ? "Select the text and press Command-C" : label}
      aria-label={label}
      onClick={() => {
        void copyText(text)
          .then(() => {
            setCopied(true);
            setError(false);
            setTimeout(() => setCopied(false), 1600);
          })
          .catch(() => setError(true));
      }}
    >
      {copied ? <Check size={13} /> : <Copy size={13} />}
      <span>{copied ? "Copied" : error ? "Select and ⌘C" : label}</span>
    </button>
  );
}
