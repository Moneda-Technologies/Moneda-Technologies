import { authApi } from "../api";
import { refreshIcons } from "../components/icons";
import { toast } from "../components/toast";
import { appStore } from "../state/store";

function authFrame(title: string, copy: string, content: string): HTMLElement {
  const root = document.createElement("main");
  root.className = "auth-page";
  root.innerHTML = `<section class="auth-brand"><img src="${appStore.state.brandLogoPath}" alt="${appStore.state.brandName}"><div class="auth-message"><span class="eyebrow">Moneda Technologies</span><h1>Secure access.<br>Clear commercial work.</h1><p>Account verification, password security and business access remain server controlled.</p></div><div class="auth-stripe"><span></span><span></span><span></span></div></section><section class="auth-form-panel"><div class="auth-form-wrap"><a class="back-link" href="/login"><i data-lucide="arrow-left"></i>Back to sign in</a><span class="eyebrow">Account access</span><h2>${title}</h2><p>${copy}</p>${content}</div><footer><span>© ${new Date().getFullYear()} Moneda Technologies</span></footer></section>`;
  refreshIcons(root); return root;
}

export function signupPage(onAuthenticated: () => Promise<void>): HTMLElement {
  const root = authFrame("Create your account", "Verify your business email, then complete your profile and password.", `<form id="signup-email" class="auth-form"><label>Email address<div class="input-icon"><i data-lucide="mail"></i><input name="email" type="email" required autocomplete="email"></div></label><button class="button button-primary button-full">Send verification code<i data-lucide="arrow-right"></i></button></form><form id="signup-otp" class="auth-form hidden"><label>Six-digit code<input name="code" class="otp-input" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required></label><button class="button button-primary button-full">Verify email<i data-lucide="arrow-right"></i></button></form><form id="signup-profile" class="auth-form hidden"><label>Full name<input name="name" minlength="2" required autocomplete="name"></label><label>Phone<input name="phone" minlength="5" required autocomplete="tel"></label><label>Password<input name="password" type="password" minlength="10" required autocomplete="new-password"></label><button class="button button-primary button-full">Create account<i data-lucide="user-check"></i></button></form>`);
  const emailForm = root.querySelector<HTMLFormElement>("#signup-email")!;
  const otpForm = root.querySelector<HTMLFormElement>("#signup-otp")!;
  const profileForm = root.querySelector<HTMLFormElement>("#signup-profile")!;
  let email = "";
  emailForm.addEventListener("submit", async (event) => {
    event.preventDefault(); email = String(new FormData(emailForm).get("email") ?? "").trim();
    try { await authApi.requestOtp(email, "signup"); emailForm.classList.add("hidden"); otpForm.classList.remove("hidden"); toast("Verification code requested", "info"); }
    catch (error) { toast(error instanceof Error ? error.message : "Could not request a code", "error"); }
  });
  otpForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await authApi.verifyOtp(email, String(new FormData(otpForm).get("code") ?? ""), "signup"); otpForm.classList.add("hidden"); profileForm.classList.remove("hidden"); }
    catch (error) { toast(error instanceof Error ? error.message : "Verification failed", "error"); }
  });
  profileForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const data = new FormData(profileForm);
    try { await authApi.register({ name: String(data.get("name")), phone: String(data.get("phone")), password: String(data.get("password")) }); await onAuthenticated(); }
    catch (error) { toast(error instanceof Error ? error.message : "Account could not be created", "error"); }
  });
  return root;
}

export function passwordResetPage(): HTMLElement {
  const root = authFrame("Reset your password", "A secure verification code will be sent to the registered email.", `<form id="reset-email" class="auth-form"><label>Registered email<div class="input-icon"><i data-lucide="mail"></i><input name="email" type="email" required autocomplete="email"></div></label><button class="button button-primary button-full">Send reset code<i data-lucide="arrow-right"></i></button></form><form id="reset-otp" class="auth-form hidden"><label>Six-digit code<input name="code" class="otp-input" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required></label><button class="button button-primary button-full">Verify code<i data-lucide="arrow-right"></i></button></form><form id="reset-password" class="auth-form hidden"><label>New password<input name="password" type="password" minlength="10" required autocomplete="new-password"></label><label>Confirm password<input name="confirm" type="password" minlength="10" required autocomplete="new-password"></label><button class="button button-primary button-full">Set new password<i data-lucide="shield-check"></i></button></form>`);
  const emailForm = root.querySelector<HTMLFormElement>("#reset-email")!;
  const otpForm = root.querySelector<HTMLFormElement>("#reset-otp")!;
  const passwordForm = root.querySelector<HTMLFormElement>("#reset-password")!;
  let email = "";
  emailForm.addEventListener("submit", async (event) => {
    event.preventDefault(); email = String(new FormData(emailForm).get("email") ?? "").trim();
    try { await authApi.requestOtp(email, "reset"); emailForm.classList.add("hidden"); otpForm.classList.remove("hidden"); toast("Reset code requested", "info"); }
    catch (error) { toast(error instanceof Error ? error.message : "Could not request a code", "error"); }
  });
  otpForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await authApi.verifyOtp(email, String(new FormData(otpForm).get("code") ?? ""), "reset"); otpForm.classList.add("hidden"); passwordForm.classList.remove("hidden"); }
    catch (error) { toast(error instanceof Error ? error.message : "Verification failed", "error"); }
  });
  passwordForm.addEventListener("submit", async (event) => {
    event.preventDefault(); const data = new FormData(passwordForm); const password = String(data.get("password") ?? "");
    if (password !== String(data.get("confirm") ?? "")) { toast("Passwords do not match", "error"); return; }
    try { await authApi.resetPassword(password); toast("Password changed successfully"); window.location.assign("/login"); }
    catch (error) { toast(error instanceof Error ? error.message : "Password could not be changed", "error"); }
  });
  return root;
}
