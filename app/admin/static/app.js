// OctoFlux dashboard core: auth, API wrapper, config save/conflict handling, hash router, UI helpers.

const KEY_STORAGE = "octoflux.admin";
const pages = new Map();
let cleanup = null;

export const icons = {
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg>',
  logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 17l-5-5 5-5"/><path d="M5 12h11"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
  up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="m6 15 6-6 6 6"/></svg>',
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>',
  close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/></svg>',
};

// ---------- small helpers ----------

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export const fmt = {
  int: (n) => (n == null ? "–" : Math.round(n).toLocaleString()),
  ms: (n) => (n == null ? "–" : n >= 1000 ? `${(n / 1000).toFixed(2)} s` : `${Math.round(n)} ms`),
  time: (epoch) => (epoch ? new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–"),
  date: (iso) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }),
  ago: (epoch) => {
    if (!epoch) return "never";
    const s = Math.max(0, Date.now() / 1000 - epoch);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  },
};

const STATUS_CLASS = { healthy: "ok", working: "ok", degraded: "warn", cooldown: "warn", unknown: "", unhealthy: "bad", disabled: "" };

export function statusBadge(status) {
  return `<span class="badge ${STATUS_CLASS[status] ?? ""}">${esc(status)}</span>`;
}

export function isEnvRef(value) {
  return typeof value === "string" && value.includes("${");
}

export function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

// ---------- API ----------

export class ApiError extends Error {
  constructor(status, body) {
    super(body?.error?.message || body?.detail || `Request failed (${status})`);
    this.status = status;
    this.body = body;
  }
}

export async function api(path, { method = "GET", body } = {}) {
  const token = sessionStorage.getItem(KEY_STORAGE);
  const headers = { Authorization: `Bearer ${token || ""}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json().catch(() => null);
  if (response.status === 401 || response.status === 403) {
    showLogin(data?.error?.type === "admin_disabled"
      ? "Admin access is off: add server.admin_keys to the config and restart."
      : "That admin key was not accepted. Enter it again.");
    throw new ApiError(response.status, data);
  }
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

export const loadConfig = () => api("/admin/config");

/** Saves the whole config. Returns the new version; on 422 throws with field errors; on 409 offers a reload. */
export async function saveConfig(version, config) {
  try {
    const { version: next } = await api("/admin/config", { method: "PUT", body: { version, config } });
    toast("Saved. The gateway is using the new settings.", "ok");
    return next;
  } catch (err) {
    if (err.status === 409) {
      const reload = await confirmDialog({
        title: "Someone else saved first",
        message: "The config changed after this page loaded. Reload to see the latest version. Unsaved edits on this page will be lost.",
        confirm: "Reload",
      });
      if (reload) rerender();
    } else if (err.status === 422) {
      toast(err.body?.error?.errors ? "Some fields need fixing before this can be saved." : err.message, "error");
    } else {
      toast(err.message, "error");
    }
    throw err;
  }
}

/** Marks inputs whose data-path matches a 422 error location, e.g. "providers.groq.base_url". */
export function showFieldErrors(root, err) {
  root.querySelectorAll(".field .error").forEach((n) => n.remove());
  root.querySelectorAll(".invalid").forEach((n) => n.classList.remove("invalid"));
  for (const e of err?.body?.error?.errors || []) {
    const path = e.loc.join(".");
    const input = [...root.querySelectorAll("[data-path]")].find((n) => path === n.dataset.path || path.startsWith(`${n.dataset.path}.`));
    if (!input) continue;
    input.classList.add("invalid");
    input.closest(".field")?.insertAdjacentHTML("beforeend", `<span class="error">${esc(e.msg)}</span>`);
  }
}

// ---------- UI primitives ----------

export function toast(message, kind = "") {
  const box = document.querySelector(".toasts");
  const node = el(`<div class="toast ${kind}" role="status">${esc(message)}</div>`);
  box.append(node);
  setTimeout(() => node.remove(), kind === "error" ? 7000 : 3500);
}

export function confirmDialog({ title, message, confirm = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const dialog = el(`
      <dialog>
        <form method="dialog" class="dialog-body">
          <h2>${esc(title)}</h2>
          <p>${esc(message)}</p>
          <div class="row end">
            <button class="btn" value="cancel">Cancel</button>
            <button class="btn ${danger ? "accent" : "primary"}" value="ok">${esc(confirm)}</button>
          </div>
        </form>
      </dialog>`);
    document.body.append(dialog);
    dialog.addEventListener("close", () => { resolve(dialog.returnValue === "ok"); dialog.remove(); });
    dialog.showModal();
  });
}

export function openDrawer(html) {
  closeDrawer();
  const scrim = el('<div class="drawer-scrim"></div>');
  const drawer = el(`<aside class="drawer" role="dialog" aria-modal="true">${html}</aside>`);
  scrim.addEventListener("click", closeDrawer);
  document.body.append(scrim, drawer);
  requestAnimationFrame(() => drawer.classList.add("open"));
  drawer.querySelector("[data-close]")?.addEventListener("click", closeDrawer);
  drawer.querySelector("[data-close]")?.focus();
  document.addEventListener("keydown", escCloses);
  return drawer;
}

function escCloses(event) {
  if (event.key === "Escape") closeDrawer();
}

export function closeDrawer() {
  document.querySelectorAll(".drawer, .drawer-scrim").forEach((n) => n.remove());
  document.removeEventListener("keydown", escCloses);
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard.");
  } catch {
    toast("Copy failed. Select the text and copy it manually.", "error");
  }
}

// ---------- router ----------

/** page = {title, subtitle?, render(main, head) -> optional cleanup fn} */
export function register(route, page) {
  pages.set(route, page);
}

function currentRoute() {
  const route = location.hash.replace(/^#\/?/, "").split("?")[0];
  return pages.has(route) ? route : "overview";
}

export async function rerender() {
  if (cleanup) { cleanup(); cleanup = null; }
  closeDrawer();
  const route = currentRoute();
  const page = pages.get(route);
  document.querySelectorAll(".tabs a").forEach((a) => {
    if (a.dataset.route === route) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.title = `${page.title} · OctoFlux`;
  const view = document.querySelector("#view");
  view.innerHTML = `
    <header class="page-head">
      <div><h1>${esc(page.title)}</h1>${page.subtitle ? `<p>${esc(page.subtitle)}</p>` : ""}</div>
      <div class="page-actions"></div>
    </header>
    <div class="page-body"><div class="empty">Loading…</div></div>`;
  try {
    cleanup = (await page.render(view.querySelector(".page-body"), view.querySelector(".page-actions"))) || null;
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) return;
    view.querySelector(".page-body").innerHTML = `<div class="card empty"><strong>This page could not load.</strong>${esc(err.message)}</div>`;
    console.warn(err);
  }
}

// ---------- shell ----------

const TABS = [
  ["overview", "Overview"], ["requests", "Requests"], ["providers", "Providers"], ["keys", "Keys"],
  ["models", "Models"], ["aliases", "Aliases"], ["playground", "Playground"], ["settings", "Settings"],
];

const LOGO = `<svg viewBox="0 0 32 32" aria-hidden="true">
  <circle cx="16" cy="13" r="9" fill="#ef6a24"/>
  <path d="M8 18c-2 4-3 7-5 9M12.5 21c-.5 3-1 6-3 8M19.5 21c.5 3 1 6 3 8M24 18c2 4 3 7 5 9" stroke="#ef6a24" stroke-width="2.6" stroke-linecap="round" fill="none"/>
  <circle cx="13" cy="12" r="1.6" fill="#fff8f0"/><circle cx="19" cy="12" r="1.6" fill="#fff8f0"/>
</svg>`;

function renderShell() {
  document.querySelector("#app").innerHTML = `
    <div class="shell">
      <nav class="topbar" aria-label="Main">
        <a class="brand" href="#/overview">${LOGO}<span>OctoFlux</span></a>
        <div class="tabs">${TABS.map(([r, label]) => `<a href="#/${r}" data-route="${r}">${label}</a>`).join("")}</div>
        <div class="top-actions">
          <button class="icon-btn" data-action="refresh" title="Refresh this page" aria-label="Refresh this page">${icons.refresh}</button>
          <button class="icon-btn" data-action="logout" title="Sign out" aria-label="Sign out">${icons.logout}</button>
        </div>
      </nav>
      <main id="view"></main>
    </div>
    <div class="toasts" aria-live="polite"></div>`;
  document.querySelector('[data-action="refresh"]').addEventListener("click", rerender);
  document.querySelector('[data-action="logout"]').addEventListener("click", () => {
    sessionStorage.removeItem(KEY_STORAGE);
    showLogin("Signed out.");
  });
}

export function showLogin(message = "") {
  if (cleanup) { cleanup(); cleanup = null; }
  document.querySelector("#app").innerHTML = `
    <div class="login">
      <form class="card">
        <div class="brand">${LOGO}<span>OctoFlux</span></div>
        <h1>Sign in to the dashboard</h1>
        <p>Use an admin key from <code>server.admin_keys</code>. Client keys only work on the <code>/v1</code> API.</p>
        <label class="field"><span>Admin key</span><input type="password" name="key" autocomplete="current-password" required autofocus></label>
        ${message ? `<p role="alert">${esc(message)}</p>` : ""}
        <button class="btn primary" type="submit">Sign in</button>
      </form>
    </div>
    <div class="toasts" aria-live="polite"></div>`;
  document.querySelector(".login form").addEventListener("submit", async (event) => {
    event.preventDefault();
    sessionStorage.setItem(KEY_STORAGE, new FormData(event.target).get("key").trim());
    try {
      await api("/admin/status");
      start();
    } catch { /* api() already re-rendered the login with a message */ }
  });
}

export function start() {
  if (!sessionStorage.getItem(KEY_STORAGE)) return showLogin();
  renderShell();
  rerender();
}

window.addEventListener("hashchange", () => {
  if (document.querySelector("#view")) rerender();
});
