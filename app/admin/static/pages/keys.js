import { api, configEditor, confirmDialog, esc, fmt, icons, isEnvRef, register, statusBadge, toast, toggle } from "../app.js";

const MODES = [
  ["", "Use the routing default"],
  ["smart", "Smart: most remaining quota first"],
  ["round_robin", "Round robin"],
  ["least_used", "Least used"],
  ["priority", "Priority: always the first key"],
];

function quota(live) {
  if (!live || live.remaining_requests == null) return '<span class="muted">Not reported</span>';
  const reset = live.resets_at ? `, resets ${new Date(live.resets_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
  return `${fmt.int(live.remaining_requests)} requests left${esc(reset)}`;
}

function valueCell(base, i, key, savedNames) {
  const path = `${base}.keys.${i}.value`;
  if (!savedNames.has(key.name) && !key.value?.startsWith("••••")) {
    return `<input type="text" data-path="${path}" data-type="str" data-required="1" value="${esc(key.value)}" placeholder="Key or \${ENV_VAR}" aria-label="Key value">`;
  }
  const shown = isEnvRef(key.value)
    ? `<span class="badge accent plain" title="Read from the server environment">from environment</span> <span class="mono">${esc(key.value)}</span>`
    : `<span class="mono">${esc(key.value)}</span>`;
  return `<span class="row">${shown}<button class="btn small ghost" data-replace="${i}">Replace</button></span>`;
}

function providerSection(pid, p, live, usage, savedNames) {
  const base = `providers.${pid}`;
  const keys = p.keys || [];
  return `
    <article class="card stack" data-provider="${esc(pid)}">
      <div class="row between">
        <h2 class="card-title" style="margin:0">${esc(pid)}</h2>
        <label class="field" style="min-width:260px"><span>Rotation</span>
          <select data-path="${base}.key_selection" data-type="str">
            ${MODES.map(([v, label]) => `<option value="${v}"${(p.key_selection || "") === v ? " selected" : ""}>${esc(label)}</option>`).join("")}
          </select></label>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Name</th><th>Value</th><th>Status</th><th>Upstream quota</th><th>Cooldown</th><th class="num">Requests</th><th>Enabled</th><th></th></tr></thead>
        <tbody>${keys.map((k, i) => {
          const saved = savedNames.has(k.name);
          const state = live?.keys?.[k.name];
          const used = usage.keys?.[`${pid}:${k.name}`]?.requests;
          return `<tr>
            <td><input type="text" data-path="${base}.keys.${i}.name" data-type="str" data-required="1" value="${esc(k.name)}" aria-label="Key name" style="min-width:150px"></td>
            <td>${valueCell(base, i, k, savedNames)}</td>
            <td>${saved ? statusBadge(state?.status || "unknown") : '<span class="badge plain">not saved yet</span>'}</td>
            <td>${saved ? quota(state) : "–"}</td>
            <td>${state?.seconds_until_available ? `${Math.ceil(state.seconds_until_available)} s` : "–"}</td>
            <td class="num">${fmt.int(used ?? 0)}</td>
            <td>${toggle("", `${base}.keys.${i}.enabled`, k.enabled !== false)}</td>
            <td><span class="row end">
              ${saved ? `<button class="btn small" data-test="${esc(k.name)}">Test</button>` : ""}
              <button class="btn small danger" data-delete="${i}" aria-label="Delete key ${esc(k.name)}">Delete</button></span></td>
          </tr>`;
        }).join("")}</tbody></table></div>
      <div class="row"><button class="btn small" data-add-key>${icons.plus}Add key</button></div>
    </article>`;
}

register("keys", {
  title: "Keys",
  subtitle: "API keys per provider. Each key is its own account with its own limits. Values are never shown in full.",
  async render(main) {
    let [status, usage] = await Promise.all([api("/admin/status"), api("/admin/usage")]);
    const { cleanup } = await configEditor(main, (config, editor) => {
      // Keys whose name exists in the saved config can be tested; renamed or new ones can't until saved.
      const savedNames = (pid) => new Set((editor.saved.providers?.[pid]?.keys || []).map((k) => k.name));
      main.innerHTML = `<div class="stack">${Object.entries(config.providers || {}).map(([pid, p]) =>
        providerSection(pid, p, status.providers[pid], usage, savedNames(pid))).join("")}</div>`;

      main.querySelectorAll("[data-provider]").forEach((section) => {
        const pid = section.dataset.provider;
        const keys = config.providers[pid].keys;
        section.querySelector("[data-add-key]").addEventListener("click", () => {
          keys.push({ name: `${pid}-${keys.length + 1}`, value: "" });
          editor.changed();
          editor.redraw();
        });
        section.querySelectorAll("[data-replace]").forEach((b) => b.addEventListener("click", () => {
          keys[Number(b.dataset.replace)].value = "";
          editor.changed();
          editor.redraw();
          section.querySelector(`[data-path="providers.${pid}.keys.${b.dataset.replace}.value"]`)?.focus();
        }));
        section.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", async () => {
          const key = keys[Number(b.dataset.delete)];
          if (!(await confirmDialog({ title: `Delete ${key.name}?`, message: "OctoFlux stops using this key once you save. The upstream account itself is not touched.", confirm: "Delete key", danger: true }))) return;
          keys.splice(Number(b.dataset.delete), 1);
          editor.changed();
          editor.redraw();
        }));
        section.querySelectorAll("[data-test]").forEach((b) => b.addEventListener("click", async () => {
          b.disabled = true;
          b.textContent = "Testing…";
          try {
            const r = await api(`/admin/providers/${encodeURIComponent(pid)}/keys/${encodeURIComponent(b.dataset.test)}/test`, { method: "POST" });
            toast(r.status === "working" ? `${b.dataset.test} works (${fmt.ms(r.latency_ms)}).` : `${b.dataset.test} failed: ${r.reason}`, r.status === "working" ? "ok" : "error");
            status = await api("/admin/status");
            editor.redraw();
          } catch (err) {
            toast(err.message, "error");
            b.disabled = false;
            b.textContent = "Test";
          }
        }));
      });
    });
    return cleanup;
  },
});
