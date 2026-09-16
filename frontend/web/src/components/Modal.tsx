import { X } from "lucide-react";
import { useEffect, useRef, type MouseEvent, type ReactNode } from "react";

export function Modal({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
    return () => dialog.current?.close();
  }, []);

  const backdropClick = (event: MouseEvent<HTMLDialogElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    if (
      event.clientX < bounds.left || event.clientX > bounds.right ||
      event.clientY < bounds.top || event.clientY > bounds.bottom
    ) onClose();
  };

  return (
    <dialog
      ref={dialog}
      className={`modal ${wide ? "modal--wide" : ""}`}
      aria-labelledby="modal-title"
      onMouseDown={backdropClick}
      onCancel={(event) => { event.preventDefault(); onClose(); }}
    >
      <header className="modal__header">
        <h2 id="modal-title">{title}</h2>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close dialog">
          <X size={18} />
        </button>
      </header>
      <div className="modal__body">{children}</div>
    </dialog>
  );
}
