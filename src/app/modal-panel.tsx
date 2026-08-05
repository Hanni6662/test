"use client";

import { useEffect, useRef, type KeyboardEvent, type ReactNode, type RefObject } from "react";

const focusableSelector = [
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[href]",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function ModalPanel({ className, labelledBy, onClose, returnFocusRef, children }: { className: string; labelledBy: string; onClose: () => void; returnFocusRef?: RefObject<HTMLElement | null>; children: ReactNode }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    openerRef.current = returnFocusRef?.current ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const panel = panelRef.current;
    const initial = panel?.querySelector<HTMLElement>("[autofocus]") ?? panel?.querySelector<HTMLElement>(focusableSelector);
    initial?.focus();
    return () => openerRef.current?.focus();
  }, [returnFocusRef]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
    if (event.key !== "Tab") return;
    const focusable = [...(panelRef.current?.querySelectorAll<HTMLElement>(focusableSelector) ?? [])];
    if (!focusable.length) { event.preventDefault(); panelRef.current?.focus(); return; }
    const first = focusable[0]; const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };

  return <div className="modal-backdrop"><div ref={panelRef} className={className} role="dialog" aria-modal="true" aria-labelledby={labelledBy} tabIndex={-1} onKeyDown={handleKeyDown}>{children}</div></div>;
}
