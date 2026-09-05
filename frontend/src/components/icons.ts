import { createElement, icons } from "lucide";

const iconAttrs = { "stroke-width": 1.8 };

function toPascalCase(value: string): string {
  return value.replace(/(\w)(\w*)(_|-|\s*)/g, (_match, first: string, rest: string) => first.toUpperCase() + rest.toLowerCase());
}

function classNames(value: string | null, iconName: string): string {
  return [...new Set(["lucide", `lucide-${iconName}`, ...(value ?? "").split(/\s+/).filter(Boolean)])].join(" ");
}

/** Replace only uninitialized icon placeholders under the rendered subtree. */
export function refreshIcons(root: HTMLElement | Document = document): void {
  const candidates: Element[] = [];
  if (root instanceof HTMLElement && root.matches("[data-lucide]")) candidates.push(root);
  candidates.push(...Array.from(root.querySelectorAll("[data-lucide]")));
  for (const element of candidates) {
    if (element.tagName.toLowerCase() === "svg" && element.classList.contains("lucide")) continue;
    const iconName = element.getAttribute("data-lucide");
    if (!iconName) continue;
    const node = icons[toPascalCase(iconName) as keyof typeof icons];
    if (!node) {
      console.warn(`${element.outerHTML} icon name was not found in the provided icons object.`);
      continue;
    }
    const attrs = Object.fromEntries(Array.from(element.attributes).map((attribute) => [attribute.name, attribute.value]));
    const [tag, baseAttrs, children] = node;
    const svg = createElement([tag, { ...baseAttrs, ...iconAttrs, ...attrs, class: classNames(attrs.class, iconName) }, children]);
    element.replaceWith(svg);
  }
}
