import { api, esc, fmt, register, statusBadge } from "../app.js";
import { bars, gradientMeter, sparkline } from "../charts.js";

function hourly(series) {
  // 1440 minutes -> 24 hourly sums of [requests, errors, tokens]
  const out = [];
  for (let h = 0; h < 24; h++) {
    const slice = series.slice(h * 60, h * 60 + 60);
    out.push(slice.reduce((acc, m) => [acc[0] + m[0], acc[1] + m[1], acc[2] + m[2]], [0, 0, 0]));
  }
  return out;
}

function lastUsed(series) {
  for (let i = series.length - 1; i >= 0; i--) if (series[i][0]) return Date.now() / 1000 - (series.length - 1 - i) * 60;
  return null;
}

function providerCard(pid, provider, series) {
  const hours = hourly(series);
  const total = hours.reduce((s, h) => s + h[0], 0);
  const errors = hours.reduce((s, h) => s + h[1], 0);
  const keys = Object.values(provider.keys);
  const working = keys.filter((k) => k.status === "healthy").length;
  return `
    <article class="card">
      <div class="card-head">
        <span class="card-meta">Last used ${esc(fmt.ago(lastUsed(series)))}</span>
        <span class="card-figure" title="Requests in the last 24 hours">${fmt.int(total)}</span>
      </div>
      <div class="row between"><h2 class="card-title">${esc(pid)}</h2>${statusBadge(provider.status)}</div>
      ${sparkline(hours.map((h) => h[0]), { compare: hours.map((h) => h[1]), title: `${pid}: requests per hour (solid) and errors per hour (dashed), last 24 hours` })}
      <div class="chart-axis"><span>24 h ago</span><span>now</span></div>
      <p class="card-meta" style="margin:12px 0 6px">${fmt.int(errors)} of them failed</p>
      <div class="row">
        <span class="badge ${working === keys.length && keys.length ? "ok" : working ? "warn" : "bad"}">${working} of ${keys.length} keys healthy</span>
        <span class="badge plain">${provider.models.filter((m) => m.enabled).length} models</span>
      </div>
    </article>`;
}

function trafficCard(allSeries) {
  const hours = Array.from({ length: 24 }, (_, h) => allSeries.reduce((sum, s) => sum + hourly(s)[h][0], 0));
  const labels = hours.map((_, h) => `${23 - h} h ago`);
  const peak = Math.max(0, ...hours);
  return `
    <article class="card">
      <div class="card-head">
        <span class="card-meta">All providers</span>
        <span class="card-figure" title="Busiest hour">${fmt.int(peak)}</span>
      </div>
      <h2 class="card-title">Traffic by hour</h2>
      ${bars(hours, labels, { title: "Requests per hour across all providers, last 24 hours", height: 150 })}
      <div class="chart-axis"><span>24 h ago</span><span>12 h</span><span>now</span></div>
    </article>`;
}

function modelsCard(usage) {
  const rows = Object.entries(usage.models || {})
    .map(([name, c]) => [name, c.requests])
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6);
  const max = Math.max(1, ...rows.map((r) => r[1]));
  return `
    <article class="card">
      <h2>Requests by model</h2>
      ${rows.length ? `<ul class="bar-list">${rows.map(([name, n]) => `
        <li style="--w:${Math.max(8, (n / max) * 100).toFixed(0)}%"><span>${esc(name)}</span><span class="num">${fmt.int(n)}</span></li>`).join("")}</ul>`
        : `<div class="empty"><strong>No requests yet</strong>Send one from the Playground to see it here.</div>`}
    </article>`;
}

function metricsStrip(usage, latency, allSeries) {
  const g = usage.global || {};
  const lastHour = allSeries.map((s) => s.slice(-60));
  const perMinute = Array.from({ length: 60 }, (_, i) => lastHour.reduce((sum, s) => sum + (s[i]?.[2] || 0), 0));
  const avgTokens = perMinute.reduce((a, b) => a + b, 0) / 60;
  const peakTokens = Math.max(0, ...allSeries.flatMap((s) => s.map((m) => m[2])));
  const p = latency.all || {};
  const successRate = g.requests ? g.successful_requests / g.requests : 0;
  return `
    <article class="card span-3">
      <div class="grid grid-3 divided">
        <div class="metric">
          <span class="metric-label">Latency</span>
          <span class="metric-value">${p.p50 == null ? "–" : `${fmt.ms(p.p50)} / req`}</span>
          <ul class="legend">
            <li><span><i class="dot" style="background:#ef6a24"></i>Average upstream</span><span>${fmt.ms(g.average_latency_ms || null)}</span></li>
            <li><span><i class="dot" style="background:#f5b54a"></i>Requests that succeeded</span><span>${Math.round(successRate * 100)}%</span></li>
          </ul>
          ${gradientMeter(successRate, { label: "Share of requests that succeeded" })}
        </div>
        <div class="metric">
          <span class="metric-label">Tokens / minute</span>
          <span class="metric-value">Peak ${fmt.int(peakTokens)}</span>
          <ul class="legend">
            <li><span><i class="dot" style="background:#9a95b2"></i>Average, last hour</span><span>${fmt.int(avgTokens)}</span></li>
            <li><span><i class="dot" style="background:#c69aa0"></i>Prompt tokens, total</span><span>${fmt.int(g.input_tokens)}</span></li>
            <li><span><i class="dot" style="background:#ef8a5a"></i>Completion tokens, total</span><span>${fmt.int(g.output_tokens)}</span></li>
          </ul>
          ${gradientMeter(peakTokens ? avgTokens / peakTokens : 0, { cool: true, label: "Average tokens per minute compared with the peak" })}
        </div>
        <div class="metric">
          <span class="metric-label">Latency percentiles</span>
          <span class="metric-value">P99 ${fmt.ms(p.p99)}</span>
          <ul class="legend">
            <li><span><i class="dot" style="background:#ef6a24"></i>P50</span><span>${fmt.ms(p.p50)}</span></li>
            <li><span><i class="dot" style="background:#f39a45"></i>P90</span><span>${fmt.ms(p.p90)}</span></li>
            <li><span><i class="dot" style="background:#f5b54a"></i>P95</span><span>${fmt.ms(p.p95)}</span></li>
            <li><span><i class="dot" style="background:#c6d96f"></i>P99</span><span>${fmt.ms(p.p99)}</span></li>
          </ul>
          ${gradientMeter(p.p99 ? p.p50 / p.p99 : 0, { label: "P50 as a share of P99" })}
        </div>
      </div>
    </article>`;
}

register("overview", {
  title: "Overview",
  async render(main) {
    const [status, usage, ts, latency] = await Promise.all([
      api("/admin/status"), api("/admin/usage"), api("/admin/timeseries?minutes=1440"), api("/admin/latency"),
    ]);
    const empty = Array.from({ length: 1440 }, () => [0, 0, 0]);
    const providers = Object.entries(status.providers).filter(([, p]) => p.enabled);
    const allSeries = Object.values(ts.providers);
    main.innerHTML = `
      <section class="grid grid-4" aria-label="Providers">
        ${providers.map(([pid, p]) => providerCard(pid, p, ts.providers[pid] || empty)).join("")}
        ${trafficCard(allSeries.length ? allSeries : [empty])}
      </section>
      <section class="grid grid-4" style="margin-top:14px" aria-label="Usage">
        ${modelsCard(usage)}
        ${metricsStrip(usage, latency, allSeries.length ? allSeries : [empty])}
      </section>`;
  },
});
