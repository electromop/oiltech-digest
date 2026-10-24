import { useEffect, useId, useRef, type ReactNode } from "react";

type Props = {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  // Широкое подокно — для разбора документа; обычное — для форм.
  wide?: boolean;
};

// Подокно поверх экрана: нативный <dialog> с showModal — фон неактивен, Escape закрывает, фокус уходит внутрь
// и возвращается на кнопку, которая подокно открыла. В jsdom showModal нет: там подокно просто открыто.
export function Dialog({ open, title, onClose, children, wide = false }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }
    if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      className={wide ? "dialog dialogWide" : "dialog"}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        // Нажатие по затемнению вокруг подокна закрывает его.
        if (event.target === event.currentTarget) onClose();
      }}
    >
      {open ? (
        <div className="dialogSurface">
          <header className="dialogHeader">
            <h2 id={titleId}>{title}</h2>
            <button type="button" className="dialogClose" aria-label="Закрыть подокно" onClick={onClose}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.6} aria-hidden="true">
                <path d="M4 4l8 8M12 4l-8 8" strokeLinecap="round" />
              </svg>
            </button>
          </header>
          <div className="dialogBody">{children}</div>
        </div>
      ) : null}
    </dialog>
  );
}
