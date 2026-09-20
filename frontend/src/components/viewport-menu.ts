const focusableSelector = 'a[href], button:not([disabled]), [role="menuitem"]';

function positionMenu(summary: HTMLElement, popover: HTMLElement): void {
  const anchor = summary.getBoundingClientRect();
  const viewportGap = 10;
  const actionGap = 6;
  const availableBelow = window.innerHeight - anchor.bottom - viewportGap;
  const availableAbove = anchor.top - viewportGap;
  const naturalHeight = Math.min(popover.scrollHeight, Math.max(160, window.innerHeight - viewportGap * 2));
  const openAbove = availableBelow < Math.min(naturalHeight, 240) && availableAbove > availableBelow;
  const maxHeight = Math.max(120, (openAbove ? availableAbove : availableBelow) - actionGap);
  const width = Math.min(Math.max(popover.scrollWidth, 190), window.innerWidth - viewportGap * 2);
  let left = anchor.right - width;
  if (left < viewportGap) left = viewportGap;
  if (left + width > window.innerWidth - viewportGap) left = window.innerWidth - viewportGap - width;
  const top = openAbove
    ? Math.max(viewportGap, anchor.top - Math.min(naturalHeight, maxHeight) - actionGap)
    : Math.min(window.innerHeight - viewportGap, anchor.bottom + actionGap);
  Object.assign(popover.style, {
    left: `${Math.round(left)}px`,
    top: `${Math.round(top)}px`,
    width: `${Math.round(width)}px`,
    maxHeight: `${Math.round(maxHeight)}px`,
  });
  popover.dataset.placement = openAbove ? "top" : "bottom";
}

export function closeViewportMenus(): void {
  document.querySelectorAll<HTMLElement>(".viewport-action-menu").forEach((popover) => {
    const ownerId = popover.dataset.ownerId;
    const owner = ownerId ? document.getElementById(ownerId) as HTMLDetailsElement | null : null;
    if (owner?.isConnected) owner.append(popover);
    if (owner) owner.open = false;
    popover.classList.remove("viewport-action-menu");
    popover.removeAttribute("style");
  });
}

export function enhanceViewportMenus(root: ParentNode): void {
  root.querySelectorAll<HTMLDetailsElement>("details.table-action-menu").forEach((details) => {
    if (details.dataset.viewportMenuReady === "true") return;
    details.dataset.viewportMenuReady = "true";
    if (!details.id) details.id = `action-menu-${crypto.randomUUID()}`;
    const summary = details.querySelector<HTMLElement>(":scope > summary");
    const popover = details.querySelector<HTMLElement>(":scope > .table-action-menu__popover");
    if (!summary || !popover) return;
    summary.setAttribute("aria-haspopup", "menu");
    summary.setAttribute("aria-expanded", "false");
    popover.setAttribute("role", "menu");
    popover.querySelectorAll<HTMLElement>(focusableSelector).forEach((item) => item.setAttribute("role", "menuitem"));

    const close = (restoreFocus = false) => {
      if (popover.parentElement === document.body) details.append(popover);
      popover.classList.remove("viewport-action-menu");
      popover.removeAttribute("style");
      details.open = false;
      summary.setAttribute("aria-expanded", "false");
      if (restoreFocus) summary.focus();
    };
    const open = () => {
      closeViewportMenus();
      details.open = true;
      summary.setAttribute("aria-expanded", "true");
      popover.dataset.ownerId = details.id;
      popover.classList.add("viewport-action-menu");
      document.body.append(popover);
      positionMenu(summary, popover);
    };

    summary.addEventListener("click", (event) => {
      event.preventDefault();
      if (popover.parentElement === document.body) close(); else open();
    });
    summary.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "Enter", " "].includes(event.key)) return;
      event.preventDefault();
      open();
      popover.querySelector<HTMLElement>(focusableSelector)?.focus();
    });
    popover.addEventListener("click", (event) => {
      if ((event.target as HTMLElement).closest(focusableSelector)) close();
    });
    popover.addEventListener("keydown", (event) => {
      const items = [...popover.querySelectorAll<HTMLElement>(focusableSelector)];
      const index = items.indexOf(document.activeElement as HTMLElement);
      if (event.key === "Escape") { event.preventDefault(); close(true); }
      else if (event.key === "ArrowDown") { event.preventDefault(); items[(index + 1) % items.length]?.focus(); }
      else if (event.key === "ArrowUp") { event.preventDefault(); items[(index - 1 + items.length) % items.length]?.focus(); }
      else if (event.key === "Home") { event.preventDefault(); items[0]?.focus(); }
      else if (event.key === "End") { event.preventDefault(); items.at(-1)?.focus(); }
    });
    document.addEventListener("pointerdown", (event) => {
      if (popover.parentElement !== document.body) return;
      const target = event.target as Node;
      if (!popover.contains(target) && !summary.contains(target)) close();
    });
    window.addEventListener("resize", () => { if (popover.parentElement === document.body) positionMenu(summary, popover); }, { passive: true });
    window.addEventListener("scroll", () => { if (popover.parentElement === document.body) positionMenu(summary, popover); }, { passive: true, capture: true });
  });
}
