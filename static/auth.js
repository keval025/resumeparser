// Sign-in helpers shared by /login and /app.
// Needs /config.js (window.APP_CONFIG) and, when login is on, supabase-js from the CDN.
// Only the public "anon" key is used here; the service-role key stays on the server.
"use strict";
(function () {
  const cfg = window.APP_CONFIG || { authEnabled: false };
  let client = null;
  if (cfg.authEnabled) {
    if (!window.supabase || !window.supabase.createClient) {
      document.addEventListener("DOMContentLoaded", () => {
        document.body.insertAdjacentHTML("afterbegin",
          '<div style="padding:12px 16px;background:#fde8e7;color:#b42318;font:14px system-ui">Could not load the sign-in library. Check your connection and reload the page.</div>');
      });
    } else {
      client = window.supabase.createClient(cfg.supabaseUrl, cfg.supabaseAnonKey, {
        // PKCE: the login redirect comes back with ?code=... (the app uses #hash for its tabs).
        auth: { flowType: "pkce", persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
      });
    }
  }

  async function currentSession() {
    if (!client) return null;
    const { data } = await client.auth.getSession();
    return data.session;
  }

  function loginUrl(reason) {
    const next = location.pathname + location.hash;
    return `/login?next=${encodeURIComponent(next)}${reason ? "&reason=" + reason : ""}`;
  }

  function safeNext(next) {
    // Only same-site paths: never redirect to another site after login.
    return typeof next === "string" && /^\/(?!\/)/.test(next) ? next : "/app";
  }

  // Supabase errors -> a sentence a user can act on. Codes first (supabase-js >= 2.43), then message text.
  function friendlyError(error) {
    if (!error) return "";
    const code = error.code || "";
    const text = (error.message || "").toLowerCase();
    if (code === "invalid_credentials" || text.includes("invalid login credentials"))
      return "Wrong email or password.";
    if (code === "email_not_confirmed" || text.includes("email not confirmed"))
      return "Please confirm your email first: open the link we sent you.";
    if (code === "user_already_exists" || code === "email_exists" || text.includes("already registered"))
      return "An account with this email already exists. Log in instead, or use \"Forgot password?\".";
    if (code === "weak_password" || text.includes("password should"))
      return "That password is too weak. Use at least 8 characters with letters and numbers.";
    if (code === "same_password") return "The new password must be different from the old one.";
    if (code.startsWith("over_") || error.status === 429 || text.includes("rate limit"))
      return "Too many attempts. Please wait a minute and try again.";
    if (code === "otp_expired" || text.includes("expired")) return "This link has expired. Please request a new one.";
    if (code === "signup_disabled") return "New sign-ups are currently turned off.";
    if (code === "email_address_invalid") return "Please enter a valid email address.";
    return error.message || "Something went wrong. Please try again.";
  }

  window.Auth = {
    enabled: !!cfg.authEnabled,
    client,
    safeNext,
    friendlyError,
    MIN_PASSWORD: 8,
    session: currentSession,

    async token() {
      const s = await currentSession();
      return s ? s.access_token : null;
    },

    // On /app: wait for the session (finishing a login redirect if needed) or go to /login.
    async requireLogin() {
      if (!cfg.authEnabled) return null;
      const s = await currentSession();
      if (!s) {
        location.replace(loginUrl());
        return new Promise(() => {}); // stop here while the browser navigates
      }
      return s;
    },

    signInWithGoogle(next) {
      return client.auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: `${location.origin}${safeNext(next)}`, queryParams: { prompt: "select_account" } },
      });
    },

    signInWithEmail(email, next) {
      return client.auth.signInWithOtp({ email, options: { emailRedirectTo: `${location.origin}${safeNext(next)}` } });
    },

    signInWithPassword(email, password) {
      return client.auth.signInWithPassword({ email, password });
    },

    // With "Confirm email" on (Supabase default) there is no session until the emailed link is opened.
    // An already-registered email comes back without an error but with no identities (anti-enumeration).
    async signUpWithPassword(email, password, next) {
      const result = await client.auth.signUp({ email, password, options: { emailRedirectTo: `${location.origin}${safeNext(next)}` } });
      const user = result.data && result.data.user;
      if (!result.error && user && Array.isArray(user.identities) && user.identities.length === 0) {
        result.error = { code: "user_already_exists", message: "User already registered" };
      }
      return result;
    },

    resendConfirmation(email, next) {
      return client.auth.resend({ type: "signup", email, options: { emailRedirectTo: `${location.origin}${safeNext(next)}` } });
    },

    sendPasswordReset(email) {
      return client.auth.resetPasswordForEmail(email, { redirectTo: `${location.origin}/reset-password` });
    },

    updatePassword(password) {
      return client.auth.updateUser({ password });
    },

    async signOut() {
      if (client) {
        try { await client.auth.signOut(); } catch { /* already signed out */ }
      }
      location.replace("/");
    },

    // fetch() with the access token. A 401 means the session is gone: back to the login page.
    async fetch(path, options) {
      const opts = Object.assign({}, options || {});
      const token = await this.token();
      if (token) opts.headers = Object.assign({}, opts.headers || {}, { Authorization: `Bearer ${token}` });
      const response = await fetch(path, opts);
      if (response.status === 401 && cfg.authEnabled) {
        try { await client.auth.signOut({ scope: "local" }); } catch { /* ignore */ }
        location.replace(loginUrl("expired"));
        return new Promise(() => {});
      }
      return response;
    },
  };

  if (client) {
    client.auth.onAuthStateChange((event) => {
      if (event === "SIGNED_OUT" && location.pathname === "/app") location.replace("/login");
    });
  }
})();
