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

  window.Auth = {
    enabled: !!cfg.authEnabled,
    client,
    safeNext,
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
