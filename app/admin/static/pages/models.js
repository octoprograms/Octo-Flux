import { api, configEditor, esc, fmt, icons, register, toggle } from "../app.js";

function staticSection(pid, p, stale) {
  const base = `providers.${pid}`;
  return `
    <article class="card stack" data-provider="${esc(pid)}">
      <h2 class="card-title" style="margin:0">${esc(pid)}</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>Model ID</th><th>Priority</th><th>Chat</th><th>Embeddings</th><th>Enabled</th><th></th></tr></thead>
        <tbody>${(p.models || []).map((m, i) => {
          const caps = m.capabilities || ["chat"];
          return `<tr>
            <td><input type="text" data-path="${base}.models.${i}.id" data-type="str" data-required="1" value="${esc(m.id)}" aria-label="Model ID" style="min-width:240px">
              ${stale.has(m.id) ? '<br><span class="badge warn" style="margin-top:6px">No longer listed as free upstream, skipped</span>' : ""}</td>
            <td><input type="number" data-path="${base}.models.${i}.priority" data-type="int" value="${esc(m.priority ?? "")}" placeholder="100" aria-label="Priority" style="width:90px"></td>
            ${["chat", "embeddings"].map((cap) => `<td><label class="switch"><input type="checkbox" data-cap="${cap}" data-index="${i}"${caps.includes(cap) ? " checked" : ""} aria-label="${cap}"></label></td>`).join("")}
            <td>${toggle("", `${base}.models.${i}.enabled`, m.enabled !== false)}</td>
            <td><button class="btn small danger" data-delete="${i}" aria-label="Delete model ${esc(m.id)}">Delete</button></td>
          </tr>`;
        }).join("")}</tbody></table></div>
      <div class="row"><button class="btn small" data-add-model>${icons.plus}Add model</button></div>
    </article>`;
}

function freeSection(free, settings) {
  if (!free) return "";
  const rows = [...free.models].sort((a, b) => (a.rank ?? 1e9) - (b.rank ?? 1e9) || a.position - b.position);
  return `
    <article class="card stack" data-free>
      <div class="row between">
        <div><h2 class="card-title" style="margin:0">Free models on ${esc(free.provider)}</h2>
          <p class="muted" style="margin:4px 0 0">Ranked by OpenRouter's order, then by how each model actually did here over the last 24 hours. Pinned models are tried first; blocked ones never.</p></div>
        <span class="card-meta">${free.discovered_at ? `Checked ${esc(fmt.ago(free.discovered_at))}` : "Discovery has not succeeded yet"}</span>
      </div>
      ${rows.length ? `<div class="table-wrap"><table>
        <thead><tr><th class="num">Rank</th><th>Model</th><th class="num">Context</th><th>Tools</th><th class="num">OK / failed (24 h)</th><th class="num">Median latency</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows.map((m) => {
          const pinned = settings.pinned.includes(m.id);
          const blocked = settings.blocked.includes(m.id);
          return `<tr>
            <td class="num">${m.rank ?? "–"}</td>
            <td>${esc(m.id)}</td>
            <td class="num">${m.context_length ? `${Math.round(m.context_length / 1024)}k` : "–"}</td>
            <td>${m.tools ? "Yes" : "No"}</td>
            <td class="num"><span class="delta-up">${m.successes_24h}</span> / <span class="delta-down">${m.failures_24h}</span></td>
            <td class="num">${fmt.ms(m.p50_ms)}</td>
            <td>${blocked ? '<span class="badge bad">blocked</span>' : pinned ? '<span class="badge accent">pinned</span>' : m.rejected_reason ? `<span class="badge plain">skipped: ${esc(m.rejected_reason)}</span>` : '<span class="badge ok">in rotation</span>'}</td>
            <td><span class="row end">
              <button class="btn small" data-pin="${esc(m.id)}">${pinned ? "Unpin" : "Pin"}</button>
              <button class="btn small${blocked ? "" : " danger"}" data-block="${esc(m.id)}">${blocked ? "Unblock" : "Block"}</button></span></td>
          </tr>`;
        }).join("")}</tbody></table></div>`
        : `<div class="empty"><strong>No free models discovered</strong>OctoFlux keeps using the configured models and the openrouter/free router until discovery works again.</div>`}
    </article>`;
}

register("models", {
  title: "Models",
  subtitle: "Configured models per provider, and the free OpenRouter models OctoFlux finds and ranks on its own.",
  async render(main) {
    const free = await api("/admin/free-models").catch(() => null);
    const stale = new Set(free?.stale || []);
    const { cleanup } = await configEditor(main, (config, editor) => {
      const freeProvider = free && config.providers?.[free.provider];
      if (freeProvider) {
        freeProvider.dynamic_models = freeProvider.dynamic_models || {};
        freeProvider.dynamic_models.pinned = freeProvider.dynamic_models.pinned || [];
        freeProvider.dynamic_models.blocked = freeProvider.dynamic_models.blocked || [];
      }
      main.innerHTML = `<div class="stack">
        ${Object.entries(config.providers || {}).map(([pid, p]) => staticSection(pid, p, pid === free?.provider ? stale : new Set())).join("")}
        ${freeProvider ? freeSection(free, freeProvider.dynamic_models) : ""}</div>`;

      main.querySelectorAll("[data-provider]").forEach((section) => {
        const models = config.providers[section.dataset.provider].models;
        section.querySelector("[data-add-model]").addEventListener("click", () => {
          models.push({ id: "", priority: 100, enabled: true });
          editor.changed();
          editor.redraw();
        });
        section.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", () => {
          models.splice(Number(b.dataset.delete), 1);
          editor.changed();
          editor.redraw();
        }));
        section.querySelectorAll("[data-cap]").forEach((box) => box.addEventListener("change", () => {
          const model = models[Number(box.dataset.index)];
          const caps = new Set(model.capabilities || ["chat"]);
          if (box.checked) caps.add(box.dataset.cap);
          else caps.delete(box.dataset.cap);
          model.capabilities = [...caps];
          editor.changed();
        }));
      });

      const settings = freeProvider?.dynamic_models;
      const flip = (list, id) => { const i = list.indexOf(id); if (i >= 0) list.splice(i, 1); else list.push(id); };
      main.querySelectorAll("[data-pin]").forEach((b) => b.addEventListener("click", () => {
        flip(settings.pinned, b.dataset.pin);
        settings.blocked = settings.blocked.filter((id) => id !== b.dataset.pin);
        editor.changed();
        editor.redraw();
      }));
      main.querySelectorAll("[data-block]").forEach((b) => b.addEventListener("click", () => {
        flip(settings.blocked, b.dataset.block);
        settings.pinned = settings.pinned.filter((id) => id !== b.dataset.block);
        editor.changed();
        editor.redraw();
      }));
    });
    return cleanup;
  },
});
