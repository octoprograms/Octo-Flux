// Hand-written SVG charts. Each returns markup with a <title> so screen readers get a text alternative.
import { esc } from "./app.js";

let uid = 0;

function smoothPath(points) {
  if (points.length < 2) return points.length ? `M${points[0][0]},${points[0][1]}` : "";
  let d = `M${points[0][0]},${points[0][1]}`;
  for (let i = 0; i < points.length - 1; i++) {
    const [p0, p1, p2, p3] = [points[i - 1] || points[i], points[i], points[i + 1], points[i + 2] || points[i + 1]];
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}

/** Solid orange line for `values`, optional dashed grey line for `compare` (same length). */
export function sparkline(values, { compare = null, title = "Trend", width = 280, height = 90 } = {}) {
  const all = values.concat(compare || []);
  const max = Math.max(1, ...all);
  const pad = 6;
  const toPoints = (series) => series.map((v, i) => [
    pad + (i * (width - pad * 2)) / Math.max(1, series.length - 1),
    height - pad - (v / max) * (height - pad * 2),
  ]);
  const grid = [0.25, 0.5, 0.75].map((f) => `<line x1="0" x2="${width}" y1="${(height * f).toFixed(1)}" y2="${(height * f).toFixed(1)}" stroke="rgba(60,44,28,.12)" stroke-dasharray="3 5"/>`).join("");
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" preserveAspectRatio="none">
    <title>${esc(title)}</title>${grid}
    ${compare ? `<path d="${smoothPath(toPoints(compare))}" fill="none" stroke="#a8a1b8" stroke-width="1.6" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>` : ""}
    <path d="${smoothPath(toPoints(values))}" fill="none" stroke="#ef6a24" stroke-width="2.2" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
  </svg>`;
}

/** Vertical bars filled with the orange→lime gradient. */
export function bars(values, labels = [], { title = "Bars", width = 280, height = 140 } = {}) {
  const id = `g${++uid}`;
  const max = Math.max(1, ...values);
  const slot = width / Math.max(1, values.length);
  const barW = Math.max(2, slot * 0.62);
  const body = values.map((v, i) => {
    const h = Math.max(v ? 3 : 0, (v / max) * (height - 4));
    return `<rect x="${(i * slot + (slot - barW) / 2).toFixed(1)}" y="${(height - h).toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="${Math.min(3, barW / 2).toFixed(1)}" fill="url(#${id})"><title>${esc(labels[i] ?? "")}: ${v}</title></rect>`;
  }).join("");
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" preserveAspectRatio="none">
    <title>${esc(title)}</title>
    <defs><linearGradient id="${id}" x1="0" y1="1" x2="0" y2="0">
      <stop offset="0" stop-color="#ef6a24"/><stop offset=".55" stop-color="#f5b54a"/><stop offset="1" stop-color="#d6e48f"/>
    </linearGradient></defs>${body}
  </svg>`;
}

/** Horizontal meter; fraction in [0, 1]. */
export function gradientMeter(fraction, { cool = false, label = "" } = {}) {
  const pct = Math.round(Math.max(0, Math.min(1, fraction || 0)) * 100);
  return `<div class="meter${cool ? " cool" : ""}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${esc(label)}"><span style="width:${pct}%"></span></div>`;
}
