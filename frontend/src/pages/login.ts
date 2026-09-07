import { authApi } from "../api";
import { ApiError } from "../api/client";
import { authPageShell } from "../components/auth-layout";
import { refreshIcons } from "../components/icons";
import { toast } from "../components/toast";

export function loginPage(demoMode: boolean, onAuthenticated: () => Promise<void>): HTMLElement {
  const root = authPageShell(`<span class="eyebrow">Welcome to Moneda</span><h2>Sign in to your workspace</h2><p>Use your account credentials or a secure email verification code.</p><div class="auth-methods" role="tablist"><button class="active" data-auth-method="password" type="button"><i data-lucide="key-round"></i>ID / password</button><button data-auth-method="otp" type="button"><i data-lucide="mail-check"></i>Email OTP</button></div><form id="password-form" class="auth-form"><label>ID / username<div class="input-icon"><i data-lucide="user-round"></i><input name="identifier" required autocomplete="username" placeholder="Admin"></div></label><label>Password<div class="input-icon"><i data-lucide="lock-keyhole"></i><input name="password" type="password" required autocomplete="current-password" placeholder="Enter your password"></div></label><div class="auth-form-links"><a href="/forgot-password">Forgot password?</a></div><button class="button button-primary button-full" type="submit">Sign in<i data-lucide="arrow-right"></i></button></form><form id="email-form" class="auth-form hidden"><label>Email address<div class="input-icon"><i data-lucide="mail"></i><input name="email" type="email" required autocomplete="email" placeholder="you@company.com"></div></label><button class="button button-primary button-full" type="submit">Send secure code<i data-lucide="arrow-right"></i></button></form><form id="otp-form" class="auth-form hidden"><button class="back-link" type="button"><i data-lucide="arrow-left"></i>Use another email</button><label>Verification code<input name="code" class="otp-input" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required autocomplete="one-time-code" placeholder="000000"></label><button class="button button-primary button-full" type="submit">Verify and sign in<i data-lucide="arrow-right"></i></button></form>${demoMode ? '<div class="auth-divider"><span>local demo access</span></div><button id="demo-login" class="button button-demo button-full"><i data-lucide="sparkles"></i>Enter demo workspace</button><p class="demo-copy">Superadmin: <strong>Admin</strong> / <strong>123@Admin</strong></p>' : ""}<p class="auth-legal">New to Moneda? <a href="/signup">Create an account</a></p>`, true);
  const passwordForm = root.querySelector<HTMLFormElement>("#password-form")!;
  const emailForm = root.querySelector<HTMLFormElement>("#email-form")!;
  const otpForm = root.querySelector<HTMLFormElement>("#otp-form")!;
  let email = "";

  root.querySelectorAll<HTMLButtonElement>("[data-auth-method]").forEach((tab) => tab.addEventListener("click", () => {
    root.querySelectorAll("[data-auth-method]").forEach((item) => item.classList.toggle("active", item === tab));
    passwordForm.classList.toggle("hidden", tab.dataset.authMethod !== "password");
    emailForm.classList.toggle("hidden", tab.dataset.authMethod !== "otp");
    otpForm.classList.add("hidden");
  }));

  passwordForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const values = new FormData(passwordForm);
    const button = passwordForm.querySelector<HTMLButtonElement>("button[type=submit]")!;
    button.disabled = true; button.textContent = "Signing in...";
    try { await authApi.login(String(values.get("identifier") ?? "").trim(), String(values.get("password") ?? "")); await onAuthenticated(); }
    catch (error) { toast(error instanceof Error ? error.message : "Sign in failed", "error"); button.disabled = false; button.innerHTML = 'Sign in<i data-lucide="arrow-right"></i>'; refreshIcons(button); }
  });
  emailForm.addEventListener("submit", async (event) => {
    event.preventDefault(); email = String(new FormData(emailForm).get("email") ?? "").trim();
    const button = emailForm.querySelector<HTMLButtonElement>("button")!; button.disabled = true; button.textContent = "Sending code...";
    try { await authApi.requestOtp(email); emailForm.classList.add("hidden"); otpForm.classList.remove("hidden"); otpForm.querySelector<HTMLInputElement>("input")?.focus(); toast("Verification code requested", "info"); }
    catch (error) { const reference = error instanceof ApiError ? error.reference : undefined; const message = error instanceof ApiError && error.status === 429 ? "Too many verification requests. Please wait before trying again." : error instanceof ApiError && error.status === 503 ? "Unable to send verification email. Please contact your administrator." : error instanceof Error ? error.message : "Verification could not be sent"; toast(`${message}${reference ? ` Reference: ${reference}` : ""}`, "error"); }
    finally { button.disabled = false; button.innerHTML = 'Send secure code<i data-lucide="arrow-right"></i>'; refreshIcons(button); }
  });
  otpForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const code = String(new FormData(otpForm).get("code") ?? "");
    const button = otpForm.querySelector<HTMLButtonElement>("button[type=submit]")!; button.disabled = true; button.textContent = "Verifying...";
    try { await authApi.verifyOtp(email, code); await onAuthenticated(); }
    catch (error) { toast(error instanceof Error ? error.message : "Code could not be verified", "error"); button.disabled = false; button.textContent = "Verify and sign in"; }
  });
  otpForm.querySelector(".back-link")?.addEventListener("click", () => { otpForm.classList.add("hidden"); emailForm.classList.remove("hidden"); });
  root.querySelector("#demo-login")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement; button.disabled = true; button.textContent = "Preparing demo...";
    try { await authApi.demo(); await onAuthenticated(); }
    catch (error) { toast(error instanceof Error ? error.message : "Demo is unavailable", "error"); button.disabled = false; button.innerHTML = '<i data-lucide="sparkles"></i>Enter demo workspace'; refreshIcons(button); }
  });
  refreshIcons(root); return root;
}
