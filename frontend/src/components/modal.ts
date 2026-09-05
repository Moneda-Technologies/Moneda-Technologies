import { refreshIcons } from "./icons";

export function openModal(title: string, content: HTMLElement, size: "normal" | "wide" = "normal"): HTMLDialogElement {
  const dialog = document.createElement("dialog");
  const titleId = `modal-title-${crypto.randomUUID()}`;
  dialog.className = `modal ${size === "wide" ? "modal-wide" : ""}`;
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", titleId);
  dialog.innerHTML = `<div class="modal-head"><div><span class="eyebrow">Moneda workspace</span><h2 id="${titleId}">${title}</h2></div><button class="icon-button" data-close aria-label="Close dialog" title="Close dialog"><i data-lucide="x"></i></button></div>`;
  const body = document.createElement("div");
  body.className = "modal-body";
  body.append(content);
  dialog.append(body);
  const opener = document.activeElement as HTMLElement | null;
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.querySelector("[data-close]")?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { dialog.remove(); opener?.focus?.(); });
  dialog.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'));
    if (!focusable.length) return;
    const first = focusable[0]; const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  document.body.append(dialog);
  dialog.showModal();
  window.setTimeout(() => (dialog.querySelector<HTMLElement>("input, select, textarea, button:not([data-close])") ?? dialog.querySelector<HTMLElement>("[data-close]"))?.focus(), 0);
  refreshIcons(dialog);
  return dialog;
}
