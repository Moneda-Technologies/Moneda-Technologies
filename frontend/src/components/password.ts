import { refreshIcons } from "./icons";

/** Add an accessible show/hide control to every password input in a subtree. */
export function enhancePasswordFields(root: HTMLElement): void {
  root.querySelectorAll<HTMLInputElement>('input[type="password"]').forEach((input) => {
    if (input.dataset.passwordToggle === "true") return;
    input.dataset.passwordToggle = "true";

    const field = document.createElement("span");
    field.className = "password-field";
    input.parentElement?.insertBefore(field, input);
    field.append(input);

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "password-toggle";
    toggle.setAttribute("aria-label", "Show password");
    toggle.title = "Show password";
    toggle.innerHTML = '<i data-lucide="eye"></i>';
    toggle.addEventListener("click", () => {
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      toggle.setAttribute("aria-label", showing ? "Show password" : "Hide password");
      toggle.title = showing ? "Show password" : "Hide password";
      toggle.innerHTML = `<i data-lucide="${showing ? "eye" : "eye-off"}"></i>`;
      refreshIcons(toggle);
      input.focus();
    });
    field.append(toggle);
  });
  refreshIcons(root);
}
