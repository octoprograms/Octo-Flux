import { configEditor, confirmDialog, esc, icons, register, toast } from "../app.js";

function options(values, selected) {
  return values.map((v) => `<option${v === selected ? " selected" : ""}>${esc(v)}</option>`).join("");
}

function aliasCard(name, targets, config) {
  const providers = Object.keys(config.providers || {});
  return `
    <article class="card stack" data-alias="${esc(name)}">
      <div class="row between">
        <div><h2 class="card-title" style="margin:0">${esc(name)}</h2>
          <p class="muted" style="margin:2px 0 0">Clients send <span class="mono">"model": "${esc(name)}"</span>; targets are tried top to bottom.</p></div>
        <button class="btn small danger" data-remove-alias>Remove alias</button>
      </div>
      <ol class="stack" style="list-style:none;margin:0;padding:0">${targets.map((t, i) => {
        const models = (config.providers?.[t.provider]?.models || []).map((m) => m.id);
        return `<li class="row" data-index="${i}">
          <span class="badge plain">${i + 1}</span>
          <select data-field="provider" aria-label="Provider">${options(providers, t.provider)}</select>
          <select data-field="model" aria-label="Model">${options(models.includes(t.model) ? models : [t.model, ...models], t.model)}</select>
          <button class="icon-btn" data-move="-1" aria-label="Move up"${i === 0 ? " disabled" : ""}>${icons.up}</button>
          <button class="icon-btn" data-move="1" aria-label="Move down"${i === targets.length - 1 ? " disabled" : ""}>${icons.down}</button>
          <button class="icon-btn" data-remove-target aria-label="Remove target">${icons.close}</button>
        </li>`;
      }).join("")}</ol>
      <div class="row"><button class="btn small" data-add-target>${icons.plus}Add target</button></div>
    </article>`;
}

register("aliases", {
  title: "Aliases",
  subtitle: "Friendly model names that fall back through an ordered list of provider models.",
  async render(main) {
    const { cleanup } = await configEditor(main, (config, editor) => {
      config.aliases = config.aliases || {};
      main.innerHTML = `<div class="stack">
        ${Object.entries(config.aliases).map(([name, targets]) => aliasCard(name, targets, config)).join("") || '<div class="card empty"><strong>No aliases yet</strong>Create one below, for example "fast" pointing at your quickest models.</div>'}
        <form class="card row" data-new-alias>
          <label class="field" style="flex:1;min-width:200px"><span>New alias name</span><input name="name" required pattern="[A-Za-z0-9._\\-]+" placeholder="fast"></label>
          <button class="btn" type="submit" style="align-self:flex-end">${icons.plus}Create alias</button>
        </form></div>`;

      const firstTarget = () => {
        const [pid, p] = Object.entries(config.providers || {})[0] || [];
        return pid ? { provider: pid, model: p.models?.[0]?.id || "" } : null;
      };
      const change = () => { editor.changed(); editor.redraw(); };

      main.querySelector("[data-new-alias]").addEventListener("submit", (event) => {
        event.preventDefault();
        const name = new FormData(event.target).get("name").trim();
        if (config.aliases[name]) return toast(`An alias called ${name} already exists.`, "error");
        const target = firstTarget();
        if (!target) return toast("Add a provider before creating aliases.", "error");
        config.aliases[name] = [target];
        change();
      });

      main.querySelectorAll("[data-alias]").forEach((card) => {
        const name = card.dataset.alias;
        const targets = config.aliases[name];
        card.querySelector("[data-remove-alias]").addEventListener("click", async () => {
          if (!(await confirmDialog({ title: `Remove ${name}?`, message: "Clients that ask for this alias will get a model-not-found error once you save.", confirm: "Remove alias", danger: true }))) return;
          delete config.aliases[name];
          change();
        });
        card.querySelector("[data-add-target]").addEventListener("click", () => { targets.push(firstTarget()); change(); });
        card.querySelectorAll("li[data-index]").forEach((li) => {
          const i = Number(li.dataset.index);
          li.querySelectorAll("select").forEach((sel) => sel.addEventListener("change", () => {
            targets[i][sel.dataset.field] = sel.value;
            if (sel.dataset.field === "provider") targets[i].model = config.providers[sel.value]?.models?.[0]?.id || "";
            change();
          }));
          li.querySelectorAll("[data-move]").forEach((b) => b.addEventListener("click", () => {
            const j = i + Number(b.dataset.move);
            [targets[i], targets[j]] = [targets[j], targets[i]];
            change();
          }));
          li.querySelector("[data-remove-target]").addEventListener("click", () => {
            targets.splice(i, 1);
            if (!targets.length) delete config.aliases[name];
            change();
          });
        });
      });
    });
    return cleanup;
  },
});
