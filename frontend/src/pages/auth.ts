import { authApi } from "../api";
import { ApiError } from "../api/client";
import { refreshIcons } from "../components/icons";
import { toast } from "../components/toast";
import { appStore } from "../state/store";

function authFrame(title: string, copy: string, content: string): HTMLElement {
  const root = document.createElement("main");
  root.className = "auth-page";
  root.innerHTML = `<header class="auth-brand"><div class="auth-logo-wrap"><img class="auth-logo" src="${appStore.state.brandLogoPath}" alt="${appStore.state.brandName}"></div><div class="auth-tagline">Know Your Alternative</div><div class="auth-stripe" aria-hidden="true"><span></span><span></span><span></span></div></header><section class="auth-content"><div class="auth-card"><div class="auth-form-wrap"><a class="back-link" href="/login"><i data-lucide="arrow-left"></i>Back to sign in</a><span class="eyebrow">Account access</span><h2>${title}</h2><p>${copy}</p>${content}</div></div></section><footer class="auth-footer"><span>© ${new Date().getFullYear()} Moneda Technologies</span></footer>`;
  refreshIcons(root); return root;
}

function authError(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) return fallback;
  const reference = error instanceof ApiError ? error.reference : undefined;
  if (error instanceof ApiError && error.status === 429) {
    return `Too many verification requests. Please wait before trying again.${reference ? ` Reference: ${reference}` : ""}`;
  }
  if (error instanceof ApiError && error.status === 503) {
    return `Unable to send verification email. Please contact your administrator.${reference ? ` Reference: ${reference}` : ""}`;
  }
  return `${error.message}${reference ? ` Reference: ${reference}` : ""}`;
}

export function signupPage(): HTMLElement {
  const root = authFrame("Create your Moneda account", "Verify your business email, then create a secure password.", `<div class="auth-steps" aria-label="Signup progress"><span class="active">1 Details</span><span>2 Verify</span><span>3 Password</span><span>4 Complete</span></div><form id="signup-details" class="auth-form"><label>Full name<input name="name" minlength="2" required autocomplete="name" placeholder="Aarav Menon"></label><label>Username<input name="username" minlength="3" maxlength="80" pattern="[A-Za-z0-9][A-Za-z0-9._\\-]*" title="Use letters, numbers, periods, underscores or hyphens" required autocomplete="username" placeholder="aarav.menon"></label><label>Email address<div class="input-icon"><i data-lucide="mail"></i><input name="email" type="email" required autocomplete="email" placeholder="you@company.com"></div></label><button class="button button-primary button-full" type="submit">Send verification code<i data-lucide="arrow-right"></i></button></form><form id="signup-otp" class="auth-form hidden"><button id="signup-change-email" class="back-link" type="button"><i data-lucide="arrow-left"></i>Change details</button><p class="signup-callout">Verify your email</p><p class="signup-destination">We sent a six-digit code to <strong id="signup-masked-email"></strong>.</p><label>Verification code<input name="otp" class="otp-input" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required autocomplete="one-time-code" placeholder="000000"></label><button class="button button-primary button-full" type="submit">Verify email<i data-lucide="arrow-right"></i></button><button id="signup-resend" class="button button-secondary button-full" type="button">Resend code</button><p id="signup-resend-status" class="signup-destination"></p></form><form id="signup-password" class="auth-form hidden"><p class="signup-callout">Create password</p><label>Password<input name="password" type="password" minlength="10" required autocomplete="new-password"></label><label>Confirm password<input name="confirm_password" type="password" minlength="10" required autocomplete="new-password"></label><button class="button button-primary button-full" type="submit">Create account<i data-lucide="user-check"></i></button></form><div id="signup-success" class="hidden signup-success"><i data-lucide="circle-check"></i><h3>Account created successfully.</h3><p>Welcome to Moneda Technologies.</p><a class="button button-primary button-full" href="/login">Sign in</a></div><p class="auth-legal">Already have an account? <a href="/login">Sign in</a></p>`);
  const detailsForm = root.querySelector<HTMLFormElement>("#signup-details")!;
  const usernameInput = detailsForm.querySelector<HTMLInputElement>('input[name="username"]')!;
  // Modern browsers compile HTML patterns with the Unicode Sets (`v`) flag,
  // where a literal hyphen inside a class must be escaped.
  usernameInput.pattern = String.raw`[A-Za-z0-9][A-Za-z0-9._\-]*`;
  const usernameError = document.createElement("small");
  usernameError.className = "field-error hidden";
  usernameError.id = "signup-username-error";
  usernameInput.setAttribute("aria-describedby", usernameError.id);
  usernameInput.insertAdjacentElement("afterend", usernameError);
  const validateUsername = () => {
    const valid = /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(usernameInput.value) || !usernameInput.value;
    usernameInput.setCustomValidity(valid ? "" : "Username can contain letters, numbers, dot, underscore and hyphen.");
    usernameInput.toggleAttribute("aria-invalid", !valid);
    usernameError.textContent = valid ? "" : usernameInput.validationMessage;
    usernameError.classList.toggle("hidden", valid);
  };
  usernameInput.addEventListener("input", validateUsername);
  const otpForm = root.querySelector<HTMLFormElement>("#signup-otp")!;
  const passwordForm = root.querySelector<HTMLFormElement>("#signup-password")!;
  const success = root.querySelector<HTMLElement>("#signup-success")!;
  const maskedEmail = root.querySelector<HTMLElement>("#signup-masked-email")!;
  const changeEmail = root.querySelector<HTMLButtonElement>("#signup-change-email")!;
  const resend = root.querySelector<HTMLButtonElement>("#signup-resend")!;
  const resendStatus = root.querySelector<HTMLElement>("#signup-resend-status")!;
  const detailsSubmit = detailsForm.querySelector<HTMLButtonElement>("button[type=submit]")!;
  let pendingSignupId = "";
  let cooldownTimer: number | undefined;
  let sendInFlight = false;

  const show = (step: "details" | "otp" | "password" | "success") => {
    detailsForm.classList.toggle("hidden", step !== "details");
    otpForm.classList.toggle("hidden", step !== "otp");
    passwordForm.classList.toggle("hidden", step !== "password");
    success.classList.toggle("hidden", step !== "success");
    root.querySelectorAll<HTMLElement>(".auth-steps span").forEach((item, index) => item.classList.toggle("active", index <= ({ details: 0, otp: 1, password: 2, success: 3 }[step])));
    refreshIcons(root);
  };
  const sendCode = async () => {
    if (sendInFlight) return;
    sendInFlight = true;
    detailsSubmit.disabled = true;
    const data = new FormData(detailsForm);
    try {
      const result = await authApi.signupStart({ name: String(data.get("name") ?? "").trim(), username: String(data.get("username") ?? "").trim(), email: String(data.get("email") ?? "").trim(), previous_pending_signup_id: pendingSignupId || undefined });
      pendingSignupId = result.pending_signup_id;
      maskedEmail.textContent = result.masked_email;
      show("otp");
      if (cooldownTimer) window.clearInterval(cooldownTimer);
      let remaining = 60;
      resend.disabled = true;
      resendStatus.textContent = `Resend available in ${remaining}s`;
      cooldownTimer = window.setInterval(() => { remaining -= 1; resendStatus.textContent = remaining > 0 ? `Resend available in ${remaining}s` : "You can request a new code."; resend.disabled = remaining > 0; if (remaining <= 0 && cooldownTimer) { window.clearInterval(cooldownTimer); cooldownTimer = undefined; } }, 1000);
      toast("Verification code sent", "info");
    } finally {
      sendInFlight = false;
      detailsSubmit.disabled = false;
      if (!cooldownTimer) resend.disabled = false;
    }
  };
  detailsForm.addEventListener("submit", async (event) => { event.preventDefault(); try { await sendCode(); } catch (error) { toast(authError(error, "Unable to send verification email. Please try again."), "error"); } });
  otpForm.addEventListener("submit", async (event) => { event.preventDefault(); try { await authApi.signupVerifyEmail(pendingSignupId, String(new FormData(otpForm).get("otp") ?? "")); show("password"); toast("Email verified", "info"); } catch (error) { toast(error instanceof Error ? error.message : "Verification failed", "error"); } });
  resend.addEventListener("click", async () => { if (resend.disabled || sendInFlight) return; try { await sendCode(); } catch (error) { toast(authError(error, "Unable to send verification email. Please try again."), "error"); } });
  changeEmail.addEventListener("click", () => show("details"));
  passwordForm.addEventListener("submit", async (event) => { event.preventDefault(); const data = new FormData(passwordForm); const password = String(data.get("password") ?? ""); const confirmPassword = String(data.get("confirm_password") ?? ""); if (password !== confirmPassword) { toast("Password confirmation does not match.", "error"); return; } try { await authApi.signupComplete({ pending_signup_id: pendingSignupId, password, confirm_password: confirmPassword }); show("success"); } catch (error) { toast(error instanceof Error ? error.message : "Account could not be created", "error"); } });
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
    catch (error) { toast(authError(error, "Could not request a code"), "error"); }
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
