import { useEffect, useState } from "react";
import { X, ImageOff, FileText } from "lucide-react";
import type { Attachment } from "../types";
import { rpc } from "../bridge";
import { Dialog } from "./Dialog";

export function AttachmentView({
  image,
  onRemove,
}: {
  image: Attachment;
  onRemove?: () => void;
}) {
  const [src, setSrc] = useState<string>();
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const document = image.kind === "document";
  useEffect(() => {
    if (document) return;
    let live = true;
    void rpc<string>("image", {
      chat_id: image.chat_id,
      attachment_id: image.id,
    })
      .then((data) => {
        if (live) setSrc(data);
      })
      .catch(() => {
        if (live) setFailed(true);
      });
    return () => {
      live = false;
    };
  }, [image.id, image.chat_id, document]);
  return (
    <div className={`attachment ${document ? "document-attachment" : ""}`}>
      <button
        className="attachment-preview"
        title={image.name}
        aria-label={`View ${image.name}`}
        onClick={() => setOpen(true)}
        disabled={!document && !src}
      >
        {document ? (
          <>
            <FileText size={23} />
            <span className="document-name">{image.name}</span>
            <small>
              {image.extraction_notice?.includes("Partial:")
                ? "Partial text"
                : "Text ready"}
            </small>
          </>
        ) : src ? (
          <img src={src} alt={image.name} />
        ) : failed ? (
          <ImageOff size={20} />
        ) : (
          <span className="spinner" />
        )}
      </button>
      {onRemove && (
        <button
          className="attachment-remove"
          aria-label={`Remove ${image.name}`}
          onClick={onRemove}
        >
          <X size={12} />
        </button>
      )}
      {failed && <span className="missing-image">Image unavailable</span>}
      {document && image.extraction_notice && (
        <p className="attachment-note">{image.extraction_notice}</p>
      )}
      {open && (
        <Dialog title={image.name} onClose={() => setOpen(false)}>
          {document ? (
            <>
              <p className="muted">
                Extracted text available to the model. {image.extraction_notice}
              </p>
              <pre className="document-text">{image.extracted_text}</pre>
            </>
          ) : (
            <img className="image-full" src={src} alt={image.name} />
          )}
          <p className="muted">
            {!document && `${image.width} × ${image.height} · `}
            {(image.size / 1024 / 1024).toFixed(1)} MiB
          </p>
        </Dialog>
      )}
    </div>
  );
}
