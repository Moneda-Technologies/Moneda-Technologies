import { authApi } from "../api";
import { ApiError } from "../api/client";
import { authPageShell } from "../components/auth-layout";
import { refreshIcons } from "../components/icons";
import { enhancePasswordFields } from "../components/password";
import { toast } from "../components/toast";

type AuthMethod = "password" | "otp";

const SUPERADMIN_OTP_ONLY_MESSAGE = "Email OTP login is available only for Superadmin accounts. Please contact your Superadmin to change or reset your password.";

/**
 * Public authentication entry point.
 *
 * Password sign-in and email OTP sign-in are intentionally independent
 * authentication methods. A successful password login establishes the
 * authenticated session immediately; the OTP tab is an explicit, optional
 * alternative that starts with an email address only.
 */
export function loginPage(demoMode: boolean, onAuthenticated: () => Promise<void>): HTMLElement {
  const root = authPageShell(`
    <span class="eyebrow">Welcome to Moneda</span>
    <h2>Sign in to your workspace</h2>
    <p>Use your account credentials or a secure email verification code.</p>

    <div class="auth-methods" role="tablist" aria-label="Authentication method">
      <button type="button" class="active" data-auth-method="password" role="tab" aria-selected="true">
        <i data-lucide="key-round"></i><span>ID / password</span>
      </button>
      <button type="button" data-auth-method="otp" role="tab" aria-selected="false">
        <i data-lucide="mail-check"></i><span>Email OTP</span>
      </button>
    </div>

    <form id="password-form" class="auth-form" novalidate>
      <label>ID / username
        <div class="input-icon"><i data-lucide="user-round"></i><input name="identifier" required autocomplete="username" placeholder="Admin"></div>
      </label>
      <label>Password
        <div class="input-icon"><i data-lucide="lock-keyhole"></i><input name="password" type="password" required autocomplete="current-password" placeholder="Enter your password"></div>
      </label>
      <div class="auth-form-links"><a href="/forgot-password">Forgot password?</a></div>
      <button class="button button-primary button-full" type="submit">Sign in<i data-lucide="arrow-right"></i></button>
    </form>

    <form id="otp-start-form" class="auth-form hidden" novalidate>
      <p class="auth-otp-note">Enter the email address registered to your Moneda account. We will send a secure one-time code.</p>
      <label>Email address
        <div class="input-icon"><i data-lucide="mail"></i><input name="email" type="email" required autocomplete="email" placeholder="you@company.com"></div>
      </label>
      <button class="button button-primary button-full" type="submit">Send secure code<i data-lucide="arrow-right"></i></button>
    </form>

    <form id="otp-form" class="auth-form hidden" novalidate>
      <p class="signup-callout">Verify your sign-in</p>
      <p class="auth-otp-note">Enter the six-digit verification code sent to your email address.</p>
      <p id="otp-destination" class="signup-destination" role="status"></p>
      <label>Verification code
        <input name="code" class="otp-input" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" required autocomplete="one-time-code" placeholder="000000">
      </label>
      <button class="button button-primary button-full" type="submit">Verify &amp; sign in<i data-lucide="arrow-right"></i></button>
      <div class="auth-resend-row"><span>Didn't receive the code?</span><button id="resend-login-otp" class="button button-quiet" type="button">Resend code</button></div>
      <p id="otp-resend-status" class="signup-destination" role="status"></p>
      <button id="change-otp-email" class="back-link" type="button"><i data-lucide="arrow-left"></i>Change email</button>
    </form>

    ${demoMode ? '<div class="auth-divider"><span>local demo access</span></div><button id="demo-login" class="button button-demo button-full"><i data-lucide="sparkles"></i>Enter demo workspace</button><p class="demo-copy">Superadmin: <strong>Admin</strong> / <strong>123@Admin</strong></p>' : ""}
  `, true);

  const passwordForm = root.querySelector<HTMLFormElement>("#password-form")!;
  const otpStartForm = root.querySelector<HTMLFormElement>("#otp-start-form")!;
  const otpForm = root.querySelector<HTMLFormElement>("#otp-form")!;
  const otpDestination = root.querySelector<HTMLElement>("#otp-destination")!;
  const resendButton = root.querySelector<HTMLButtonElement>("#resend-login-otp")!;
  const resendStatus = root.querySelector<HTMLElement>("#otp-resend-status")!;
  const methodButtons = Array.from(root.querySelectorAll<HTMLButtonElement>("[data-auth-method]"));
  let otpEmail = "";
  let resendTimer: number | undefined;

  const maskEmail = (value: string) => {
    const [local = "", domain = ""] = value.split("@", 2);
    if (!domain) return "your email address";
    const visible = local.length <= 2 ? local.slice(0, 1) : local.slice(0, 2);
    return `${visible}${"*".repeat(Math.max(1, Math.min(6, local.length - visible.length)))}@${domain}`;
  };

  const setButtonContent = (button: HTMLButtonElement, label: string, icon?: string) => {
    button.innerHTML = `${label}${icon ? `<i data-lucide="${icon}"></i>` : ""}`;
    refreshIcons(button);
  };

  const clearResendTimer = () => {
    if (resendTimer !== undefined) window.clearInterval(resendTimer);
    resendTimer = undefined;
  };

  const startResendCooldown = (seconds = 30) => {
    clearResendTimer();
    let remaining = seconds;
    resendButton.disabled = true;
    resendButton.textContent = `Resend code in ${remaining}s`;
    resendTimer = window.setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearResendTimer();
        resendButton.disabled = false;
        resendButton.textContent = "Resend code";
      } else {
        resendButton.textContent = `Resend code in ${remaining}s`;
      }
    }, 1000);
  };

  const setMethod = (method: AuthMethod) => {
    methodButtons.forEach((button) => {
      const active = button.dataset.authMethod === method;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    passwordForm.classList.toggle("hidden", method !== "password");
    otpStartForm.classList.toggle("hidden", method !== "otp" || Boolean(otpEmail));
    otpForm.classList.toggle("hidden", method !== "otp" || !otpEmail);
  };

  const showOtpChallenge = (email: string) => {
    otpEmail = email;
    setMethod("otp");
    otpDestination.textContent = `We sent a six-digit code to ${maskEmail(email)}.`;
    resendStatus.textContent = "";
    startResendCooldown();
    otpForm.querySelector<HTMLInputElement>('input[name="code"]')?.focus();
    toast("Verification code sent to your email", "info");
  };

  const authError = (error: unknown, fallback: string) => {
    const reference = error instanceof ApiError ? error.reference : undefined;
    const message = error instanceof ApiError && error.status === 403 && /superadmin/i.test(error.message)
      ? SUPERADMIN_OTP_ONLY_MESSAGE
      : error instanceof ApiError && error.status === 503
      ? "Unable to send verification email. Please contact your administrator."
      : error instanceof Error ? error.message : fallback;
    return `${message}${reference ? ` Reference: ${reference}` : ""}`;
  };

  passwordForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = new FormData(passwordForm);
    const identifier = String(values.get("identifier") ?? "").trim();
    const password = String(values.get("password") ?? "");
    if (!identifier || !password) {
      toast("Enter your username and password.", "error");
      return;
    }
    const button = passwordForm.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    button.disabled = true;
    button.textContent = "Signing in...";
    try {
      // Password authentication is complete when the backend accepts these
      // credentials. It must not be converted into an OTP challenge.
      await authApi.login(identifier, password);
      await onAuthenticated();
    } catch (error) {
      toast(authError(error, "Sign in failed"), "error");
    } finally {
      button.disabled = false;
      setButtonContent(button, "Sign in", "arrow-right");
    }
  });

  otpStartForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const email = String(new FormData(otpStartForm).get("email") ?? "").trim().toLowerCase();
    if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      toast("Enter a valid email address.", "error");
      return;
    }
    const button = otpStartForm.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    button.disabled = true;
    button.textContent = "Sending secure code...";
    try {
      await authApi.requestOtp(email, "login");
      showOtpChallenge(email);
    } catch (error) {
      toast(authError(error, "Unable to send verification code."), "error");
    } finally {
      button.disabled = false;
      setButtonContent(button, "Send secure code", "arrow-right");
    }
  });

  methodButtons.forEach((button) => button.addEventListener("click", () => {
    setMethod((button.dataset.authMethod as AuthMethod) || "password");
  }));

  resendButton.addEventListener("click", async () => {
    if (!otpEmail || resendButton.disabled) return;
    resendButton.disabled = true;
    resendButton.textContent = "Sending...";
    resendStatus.textContent = "";
    try {
      await authApi.requestOtp(otpEmail, "login");
      resendStatus.textContent = "A new verification code was sent.";
      startResendCooldown();
    } catch (error) {
      const message = error instanceof ApiError && error.status === 429
        ? "Please wait before requesting another code."
        : authError(error, "Unable to resend verification code.");
      resendStatus.textContent = message;
      startResendCooldown(error instanceof ApiError && error.status === 429 ? 30 : 5);
    }
  });

  otpForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = String(new FormData(otpForm).get("code") ?? "");
    if (!/^\d{6}$/.test(code)) {
      toast("Enter the six-digit verification code.", "error");
      return;
    }
    const button = otpForm.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    button.disabled = true;
    button.textContent = "Verifying...";
    try {
      await authApi.verifyOtp(otpEmail, code, "login");
      clearResendTimer();
      await onAuthenticated();
    } catch (error) {
      toast(authError(error, "Code could not be verified"), "error");
      button.disabled = false;
      setButtonContent(button, "Verify & sign in", "arrow-right");
    }
  });

  root.querySelector<HTMLButtonElement>("#change-otp-email")?.addEventListener("click", () => {
    clearResendTimer();
    otpEmail = "";
    otpDestination.textContent = "";
    resendStatus.textContent = "";
    resendButton.disabled = false;
    resendButton.textContent = "Resend code";
    otpForm.querySelector<HTMLInputElement>('input[name="code"]')!.value = "";
    setMethod("otp");
    otpStartForm.querySelector<HTMLInputElement>('input[name="email"]')?.focus();
  });

  root.querySelector("#demo-login")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    button.disabled = true;
    button.textContent = "Preparing demo...";
    try {
      await authApi.demo();
      await onAuthenticated();
    } catch (error) {
      toast(error instanceof Error ? error.message : "Demo is unavailable", "error");
      button.disabled = false;
      setButtonContent(button, "Enter demo workspace", "sparkles");
    }
  });

  enhancePasswordFields(root);
  refreshIcons(root);
  return root;
}
