import { api, configEditor, confirmDialog, copyText, esc, field, fmt, icons, isEnvRef, register, rerender, toast, toggle } from "../app.js";

let justCreated = null; // a new client key, shown once after the page redraws

const MODES = [["smart", "Smart: most remaining quota first"], ["round_robin", "Round robin"], ["least_used", "Least used"], ["priority", "Priority: always the first key"]];

function secretLabel(value) {
  return isEnvRef(value) ? `<span class="badge accent plain">from environment</span> <span class="mono">${esc(value)}</span>` : `<span class="mono">${esc(value)}</span>`;
}

register("settings", {
  title: "Settings",
  subtitle: "Who can use the gateway, who can manage it, routing defaults, and earlier versions of the config.",
  async render(main) {
    const { backups } = await api("/admin/config/backups");
    const { cleanup } = await configEditor(main, (config, editor) => {
      const server = (config.server = config.server || {});
      server.client_keys = server.client_keys || [];
      server.admin_keys = server.admin_keys || [];
      const routing = config.routing || {};
      const created = justCreated;
      justCreated = null;

      main.innerHTML = `<div class="grid grid-2">
        <section class="card stack">
          <div><h2 style="margin:0">Client keys</h2><p class="muted" style="margin:4px 0 0">Your apps send one of these as <span class="mono">Authorization: Bearer</span> on <span class="mono">/v1</span>. They cannot open this dashboard.</p></div>
          ${created ? `<div class="stack" role="status"><strong>New client key. Copy it now; it won't be shown again.</strong>
            <div class="secret-once"><code>${esc(created)}</code><button class="btn small" data-copy>${icons.copy}Copy</button></div></div>` : ""}
          <table><tbody>${server.client_keys.map((k, i) => `<tr><td>${secretLabel(k)}</td>
            <td class="num"><button class="btn small danger" data-revoke="${i}">Revoke</button></td></tr>`).join("") || '<tr><td class="muted">No client keys. With require_auth on, every /v1 request is rejected.</td></tr>'}</tbody></table>
          <div class="row"><button class="btn" data-create>${icons.plus}Create client key</button></div>
        </section>

        <section class="card stack">
          <div><h2 style="margin:0">Admin keys</h2><p class="muted" style="margin:4px 0 0">People who can sign in here and change the configuration.</p></div>
          <table><tbody>${server.admin_keys.map((k, i) => `<tr><td><strong>${esc(k.name)}</strong></td><td>${secretLabel(k.value)}</td>
            <td class="num"><button class="btn small danger" data-remove-admin="${i}"${server.admin_keys.length < 2 ? ' disabled title="The last admin key cannot be removed"' : ""}>Remove</button></td></tr>`).join("")}</tbody></table>
          <form class="fields" data-add-admin>
            <label class="field"><span>Name</span><input name="name" required placeholder="teammate"></label>
            <label class="field"><span>Key</span><input name="value" required placeholder="\${TEAMMATE_ADMIN_KEY}"><small>A literal key or \${ENV_VAR}</small></label>
            <div class="row" style="align-self:end"><button class="btn" type="submit">${icons.plus}Add admin</button></div>
          </form>
        </section>

        <section class="card stack">
          <h2 style="margin:0">Routing defaults</h2>
          <label class="field"><span>Key rotation</span><select data-path="routing.default_key_selection" data-type="str">
            ${MODES.map(([v, label]) => `<option value="${v}"${(routing.default_key_selection || "smart") === v ? " selected" : ""}>${esc(label)}</option>`).join("")}</select>
            <small>Used by providers that don't set their own on the Keys page.</small></label>
          <div class="fields">${field("Max attempts per request", "routing.max_total_attempts", routing.max_total_attempts, { type: "int", placeholder: "4", hint: "Across all keys, models and providers" })}</div>
          ${toggle("Fall back to other models", "routing.allow_model_fallback", routing.allow_model_fallback !== false)}
          ${toggle("Fall back to other providers", "routing.allow_provider_fallback", routing.allow_provider_fallback !== false)}
        </section>

        <section class="card stack">
          <div><h2 style="margin:0">Config history</h2><p class="muted" style="margin:4px 0 0">A copy is kept before every save; the newest 20 are listed.</p></div>
          ${backups.length ? `<table><tbody>${backups.map((b) => `<tr><td>${esc(fmt.date(b.created_at))}</td><td class="muted">${fmt.int(b.size / 1024)} KB</td>
            <td class="num"><button class="btn small" data-restore="${esc(b.name)}">Restore</button></td></tr>`).join("")}</tbody></table>`
            : '<div class="empty"><strong>No earlier versions yet</strong>They appear here after the first save.</div>'}
        </section></div>`;

      main.querySelector("[data-copy]")?.addEventListener("click", () => copyText(created));

      main.querySelector("[data-create]").addEventListener("click", async () => {
        if (editor.dirty) return toast("Save or discard your changes first.", "error");
        try {
          justCreated = (await api("/admin/client-keys", { method: "POST" })).key;
          toast("Client key created.", "ok");
          rerender();
        } catch (err) {
          toast(err.message, "error");
        }
      });
      main.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
        if (!(await confirmDialog({ title: "Revoke this client key?", message: "Apps using it get 401 errors once you save.", confirm: "Revoke key", danger: true }))) return;
        server.client_keys.splice(Number(b.dataset.revoke), 1);
        editor.changed();
        editor.redraw();
      }));
      main.querySelectorAll("[data-remove-admin]").forEach((b) => b.addEventListener("click", async () => {
        const admin = server.admin_keys[Number(b.dataset.removeAdmin)];
        if (!(await confirmDialog({ title: `Remove ${admin.name}?`, message: "They can no longer sign in once you save.", confirm: "Remove admin", danger: true }))) return;
        server.admin_keys.splice(Number(b.dataset.removeAdmin), 1);
        editor.changed();
        editor.redraw();
      }));
      main.querySelector("[data-add-admin]").addEventListener("submit", (event) => {
        event.preventDefault();
        const data = Object.fromEntries(new FormData(event.target));
        if (server.admin_keys.some((k) => k.name === data.name.trim())) return toast(`An admin called ${data.name.trim()} already exists.`, "error");
        server.admin_keys.push({ name: data.name.trim(), value: data.value.trim() });
        editor.changed();
        editor.redraw();
      });
      main.querySelectorAll("[data-restore]").forEach((b) => b.addEventListener("click", async () => {
        const ok = await confirmDialog({
          title: "Restore this version?",
          message: "The current config is backed up first, then replaced by this version and applied right away.",
          confirm: "Restore version",
        });
        if (!ok) return;
        try {
          await api(`/admin/config/backups/${encodeURIComponent(b.dataset.restore)}/restore`, { method: "POST" });
          toast("Restored. The gateway is using that version now.", "ok");
          rerender();
        } catch (err) {
          toast(err.body?.error?.message || err.message, "error");
        }
      }));
    });
    return cleanup;
  },
});
