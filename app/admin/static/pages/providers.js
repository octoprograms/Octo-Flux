import { api, configEditor, confirmDialog, esc, field, icons, register, statusBadge, toast, toggle } from "../app.js";

const LIMIT_FIELDS = [
  ["requests_per_minute", "Requests / minute", "int"], ["requests_per_hour", "Requests / hour", "int"],
  ["requests_per_day", "Requests / day", "int"], ["requests_per_second", "Requests / second", "float"],
  ["tokens_per_minute", "Tokens / minute", "int"], ["tokens_per_hour", "Tokens / hour", "int"],
  ["tokens_per_day", "Tokens / day", "int"], ["concurrent_requests", "Concurrent requests", "int"],
];

function limitFields(base, limits) {
  return `<div class="fields">${LIMIT_FIELDS.map(([k, label, type]) => field(label, `${base}.${k}`, limits?.[k], { type, placeholder: "No limit" })).join("")}</div>`;
}

function providerCard(pid, p, live) {
  const base = `providers.${pid}`;
  const retry = p.retry || {};
  const health = p.health || {};
  return `
    <article class="card stack" data-provider="${esc(pid)}">
      <div class="row between">
        <div class="row"><h2 class="card-title" style="margin:0">${esc(pid)}</h2>${statusBadge(p.enabled === false ? "disabled" : live?.status || "unknown")}</div>
        <div class="row">${toggle("Enabled", `${base}.enabled`, p.enabled !== false)}
          <button class="btn small danger" data-remove="${esc(pid)}">Remove</button></div>
      </div>
      <div class="fields">
        ${field("Base URL", `${base}.base_url`, p.base_url, { input: "url", required: true })}
        ${field("Priority", `${base}.priority`, p.priority, { type: "int", hint: "Lower is tried first", placeholder: "100" })}
        ${field("Request timeout (s)", `${base}.request_timeout_seconds`, p.request_timeout_seconds, { type: "float", placeholder: "60" })}
      </div>
      <details><summary>Limits per key</summary>
        <p class="muted">Each key of ${esc(pid)} gets this budget. Leave a field empty for no limit.</p>
        ${limitFields(`${base}.limits`, p.limits)}</details>
      <details><summary>Total limits for all keys combined</summary>
        <p class="muted">Optional cap shared by every key of this provider.</p>
        ${limitFields(`${base}.total_limits`, p.total_limits)}</details>
      <details><summary>Retry and health</summary>
        <div class="fields" style="margin-top:10px">
          ${field("Max attempts", `${base}.retry.max_attempts`, retry.max_attempts, { type: "int", placeholder: "3" })}
          ${field("Base delay (ms)", `${base}.retry.base_delay_ms`, retry.base_delay_ms, { type: "int", placeholder: "50" })}
          ${field("Max delay (ms)", `${base}.retry.max_delay_ms`, retry.max_delay_ms, { type: "int", placeholder: "2000" })}
          ${field("Jitter (ms)", `${base}.retry.jitter_ms`, retry.jitter_ms, { type: "int", placeholder: "50" })}
          ${field("Failures before unhealthy", `${base}.health.failure_threshold`, health.failure_threshold, { type: "int", placeholder: "3" })}
          ${field("Cooldown (s)", `${base}.health.cooldown_seconds`, health.cooldown_seconds, { type: "int", placeholder: "30" })}
          ${field("Quota cooldown (s)", `${base}.health.long_cooldown_seconds`, health.long_cooldown_seconds, { type: "int", placeholder: "300" })}
        </div></details>
    </article>`;
}

function addForm() {
  return `
    <form class="card stack" data-add>
      <h2 style="margin:0">Add a provider</h2>
      <p class="muted" style="margin:0">Any OpenAI-compatible API. You can add more keys and models afterwards.</p>
      <div class="fields">
        <label class="field"><span>ID</span><input name="id" required pattern="[a-z0-9_\\-]+" placeholder="mistral"><small>Lowercase letters, digits, - and _</small></label>
        <label class="field"><span>Base URL</span><input name="base_url" type="url" required placeholder="https://api.mistral.ai/v1"></label>
        <label class="field"><span>First key</span><input name="key" required placeholder="\${MISTRAL_API_KEY}"><small>A literal key or \${ENV_VAR}</small></label>
        <label class="field"><span>First model</span><input name="model" required placeholder="mistral-small-latest"></label>
      </div>
      <div class="row"><button class="btn" type="submit">${icons.plus}Add provider</button></div>
    </form>`;
}

register("providers", {
  title: "Providers",
  subtitle: "Upstream APIs OctoFlux can route to, their priority, limits and retry behaviour.",
  async render(main) {
    const status = await api("/admin/status");
    const { cleanup } = await configEditor(main, (config, editor) => {
      main.innerHTML = `<div class="stack">
        ${Object.entries(config.providers || {}).map(([pid, p]) => providerCard(pid, p, status.providers[pid])).join("")}
        ${addForm()}</div>`;

      main.querySelectorAll("[data-remove]").forEach((button) => button.addEventListener("click", async () => {
        const pid = button.dataset.remove;
        const ok = await confirmDialog({
          title: `Remove ${pid}?`,
          message: `${pid} and its keys and models are removed, and aliases stop pointing at it. Nothing changes until you save.`,
          confirm: "Remove provider",
          danger: true,
        });
        if (!ok) return;
        delete config.providers[pid];
        for (const [alias, targets] of Object.entries(config.aliases || {})) {
          config.aliases[alias] = targets.filter((t) => t.provider !== pid);
          if (!config.aliases[alias].length) delete config.aliases[alias];
        }
        editor.changed();
        editor.redraw();
      }));

      main.querySelector("[data-add]").addEventListener("submit", (event) => {
        event.preventDefault();
        const data = Object.fromEntries(new FormData(event.target));
        const id = data.id.trim();
        if (config.providers?.[id]) return toast(`A provider called ${id} already exists.`, "error");
        config.providers = config.providers || {};
        config.providers[id] = {
          enabled: true, type: "openai_compatible", base_url: data.base_url.trim(), priority: 100,
          keys: [{ name: `${id}-1`, value: data.key.trim() }],
          models: [{ id: data.model.trim(), priority: 10 }],
        };
        editor.changed();
        editor.redraw();
        toast(`${id} added. Save changes to start using it.`);
      });
    });
    return cleanup;
  },
});
