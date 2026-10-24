import { api, esc, fmt, loadConfig, register, statusCodeBadge, traceList } from "../app.js";

function modelOptions(config) {
  const groups = [`<option value="auto">auto (best available)</option>`];
  const aliases = Object.keys(config.aliases || {});
  if (aliases.length) groups.push(`<optgroup label="Aliases">${aliases.map((a) => `<option>${esc(a)}</option>`).join("")}</optgroup>`);
  for (const [pid, p] of Object.entries(config.providers || {})) {
    if (p.enabled === false) continue;
    const models = (p.models || []).filter((m) => m.enabled !== false && (m.capabilities || ["chat"]).includes("chat"));
    if (models.length) groups.push(`<optgroup label="${esc(pid)}">${models.map((m) => `<option>${esc(m.id)}</option>`).join("")}</optgroup>`);
  }
  return groups.join("");
}

register("playground", {
  title: "Playground",
  subtitle: "Send a message through the gateway and see which provider, key and model answered.",
  async render(main) {
    const { config } = await loadConfig();
    main.innerHTML = `
      <div class="grid grid-2">
        <form class="card stack">
          <label class="field"><span>Model</span><select name="model">${modelOptions(config)}</select></label>
          <label class="field"><span>Message</span><textarea name="message" rows="7" required placeholder="Ask anything, for example: summarise the benefits of key rotation in two sentences."></textarea></label>
          <label class="field" style="max-width:200px"><span>Max tokens</span><input type="number" name="max_tokens" min="1" max="8192" placeholder="Model default"></label>
          <div class="row"><button class="btn accent" type="submit">Send message</button><span class="muted" data-status aria-live="polite"></span></div>
        </form>
        <section class="card" data-result>
          <div class="empty"><strong>No reply yet</strong>Pick a model, write a message and send it.</div>
        </section>
      </div>`;
    const form = main.querySelector("form");
    const out = main.querySelector("[data-result]");
    const status = main.querySelector("[data-status]");

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const body = { model: data.get("model"), messages: [{ role: "user", content: data.get("message") }] };
      if (data.get("max_tokens")) body.max_tokens = Number(data.get("max_tokens"));
      const button = form.querySelector("button");
      button.disabled = true;
      status.textContent = "Waiting for the gateway…";
      let reply = "", record = null, error = null;
      try {
        const res = await api("/admin/playground", { method: "POST", body });
        reply = res.response?.choices?.[0]?.message?.content ?? "";
        record = res.record;
      } catch (err) {
        error = err.message;
        record = err.body?.record ?? null;
      } finally {
        button.disabled = false;
        status.textContent = "";
      }
      out.innerHTML = `
        <div class="row between"><h2 style="margin:0">${error ? "The gateway could not answer" : "Reply"}</h2>${record ? statusCodeBadge(record.status) : ""}</div>
        <div data-reply style="white-space:pre-wrap;margin:12px 0 16px;max-height:340px;overflow:auto"></div>
        ${record ? `
          <div class="row" style="margin-bottom:14px">
            <span class="badge accent">${esc(record.provider ?? "no provider")}</span>
            <span class="badge plain">${esc(record.upstream_model ?? "–")}</span>
            <span class="badge plain">key ${esc(record.key ?? "–")}</span>
            <span class="badge plain">${fmt.ms(record.latency_ms)}</span>
            <span class="badge plain">${fmt.int(record.input_tokens + record.output_tokens)} tokens</span>
          </div>
          <h3>Attempts</h3>${traceList(record.attempts)}
          <p class="muted" style="margin-top:12px">Request ID <span class="mono">${esc(record.id)}</span>, also listed on the Requests page.</p>` : ""}`;
      out.querySelector("[data-reply]").textContent = error || reply || "(The model returned an empty reply.)";
    });
  },
});
