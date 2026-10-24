import { api, esc, fmt, icons, openDrawer, register, statusCodeBadge, toast, traceList } from "../app.js";

function row(r) {
  return `<tr class="clickable" data-id="${esc(r.id)}" tabindex="0">
    <td>${esc(fmt.time(r.started_at))}</td>
    <td>${esc(r.requested_model)}${r.stream ? ' <span class="badge plain">stream</span>' : ""}</td>
    <td>${esc(r.upstream_model ?? "–")}</td>
    <td>${esc(r.provider ?? "–")}</td>
    <td>${esc(r.key ?? "–")}</td>
    <td>${statusCodeBadge(r.status)}</td>
    <td class="num">${fmt.ms(r.latency_ms)}</td>
    <td class="num">${fmt.int(r.input_tokens + r.output_tokens)}</td>
    <td class="num">${r.attempts.length}</td>
  </tr>`;
}

async function openRequest(id) {
  let r;
  try {
    r = await api(`/admin/requests/${encodeURIComponent(id)}`);
  } catch (err) {
    toast(err.status === 404 ? `No request with ID ${id} in the last 200.` : err.message, "error");
    return;
  }
  openDrawer(`
    <div class="row between">
      <h2 style="margin:0;font-size:22px;letter-spacing:-.02em">Request ${esc(r.id)}</h2>
      <button class="icon-btn" data-close aria-label="Close">${icons.close}</button>
    </div>
    <p class="muted">${esc(new Date(r.started_at * 1000).toLocaleString())}, ${esc(r.operation)}</p>
    <div class="grid grid-2" style="margin:16px 0">
      <div class="metric"><span class="metric-label">Status</span><span>${statusCodeBadge(r.status)}</span></div>
      <div class="metric"><span class="metric-label">Total time</span><span class="metric-value">${fmt.ms(r.latency_ms)}</span></div>
      <div class="metric"><span class="metric-label">Asked for</span><span>${esc(r.requested_model)}</span></div>
      <div class="metric"><span class="metric-label">Answered by</span><span>${esc(r.provider ? `${r.provider} / ${r.upstream_model}` : "nobody")}</span></div>
      <div class="metric"><span class="metric-label">Prompt tokens</span><span>${fmt.int(r.input_tokens)}</span></div>
      <div class="metric"><span class="metric-label">Completion tokens</span><span>${fmt.int(r.output_tokens)}</span></div>
    </div>
    <h3>Attempts</h3>
    ${traceList(r.attempts)}`);
}

register("requests", {
  title: "Requests",
  subtitle: "The last 200 requests through the gateway. Message content is never stored.",
  async render(main, head) {
    head.innerHTML = `<label class="field" style="min-width:260px"><input type="search" placeholder="Enter a request ID" aria-label="Find a request by ID or model"></label>`;
    const search = head.querySelector("input");
    main.innerHTML = `<section class="card"><div class="table-wrap"><table>
      <thead><tr><th>Time</th><th>Requested model</th><th>Upstream model</th><th>Provider</th><th>Key</th><th>Status</th><th class="num">Latency</th><th class="num">Tokens</th><th class="num">Attempts</th></tr></thead>
      <tbody></tbody></table></div></section>`;
    const tbody = main.querySelector("tbody");
    let records = [];

    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const shown = q ? records.filter((r) => [r.id, r.requested_model, r.upstream_model, r.provider].some((v) => (v || "").toLowerCase().includes(q))) : records;
      tbody.innerHTML = shown.length ? shown.map(row).join("")
        : `<tr><td colspan="9"><div class="empty"><strong>${q ? "No request matches that search" : "No requests yet"}</strong>${q ? "Try part of a request ID or a model name." : "Send one from the Playground and it appears here."}</div></td></tr>`;
    };
    const load = async () => {
      records = (await api("/admin/requests")).requests;
      draw();
    };

    tbody.addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openRequest(tr.dataset.id); });
    tbody.addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr && e.key === "Enter") openRequest(tr.dataset.id); });
    search.addEventListener("input", draw);
    search.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const exact = records.find((r) => r.id === search.value.trim());
      openRequest(exact ? exact.id : search.value.trim());
    });

    await load();
    const timer = setInterval(() => { if (document.visibilityState === "visible") load().catch(() => {}); }, 5000);
    return () => clearInterval(timer);
  },
});
