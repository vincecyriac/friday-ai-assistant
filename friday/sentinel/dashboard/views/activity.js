import { api } from "../api.js";
import { socket } from "../socket.js";
import { cls, el, fmt } from "../ui.js";

export const title = "Activity";
const MAX_ROWS = 500;
const PREFIXES = ["node", "telemetry", "audit", "config", "sentinel", "triage", "escalation", "message"];
const BADGE = {
  node: cls("badge-emerald"), telemetry: cls("badge-zinc"), audit: cls("badge-indigo"), config: cls("badge-amber"),
  sentinel: cls("badge-zinc"), triage: cls("badge-rose"), escalation: cls("badge-rose"), message: cls("badge-rose"),
};

export function summary(evt) {
  const p = evt.payload || {};
  switch (evt.type) {
    case "node.heartbeat": return `${p.status || ""} · v${p.version || "?"}`;
    case "telemetry.sample": {
      const mem = p.mem_total ? Math.round((p.mem_used / p.mem_total) * 100) : null;
      return `cpu ${p.cpu_percent === null || p.cpu_percent === undefined ? "—" : Math.round(p.cpu_percent)}% · mem ${mem === null ? "—" : mem}%`;
    }
    case "audit.entry": return `${p.actor || ""} ${p.action || ""} ${p.target || ""}`.trim();
    case "config.changed": return `${p.actor || ""}: ${(p.keys || []).join(", ")}`;
    default: return JSON.stringify(p).slice(0, 80);
  }
}

export async function mount(root) {
  const state = { rows: [], filters: new Set(), query: "", paused: false, buffered: [] };
  const list = el("div", { class: "divide-y divide-zinc-800/60" });
  const search = el("input", { class: "input md:w-64", placeholder: "Filter by type, source or text",
    oninput: () => { state.query = search.value.trim().toLowerCase(); renderList(); } });
  const pause = el("button", { class: "btn", text: "Pause", onclick: () => {
    state.paused = !state.paused;
    if (!state.paused) { state.buffered.forEach(push); state.buffered = []; }
    pause.textContent = state.paused ? "Resume" : "Pause";
  } });
  const chips = PREFIXES.map((prefix) => el("button", {
    class: cls("badge", "badge-zinc"), text: prefix,
    onclick: () => {
      if (state.filters.has(prefix)) state.filters.delete(prefix); else state.filters.add(prefix);
      chips.forEach((chip) => chip.className = cls("badge", state.filters.has(chip.textContent) ? "badge-indigo" : "badge-zinc"));
      renderList();
    },
  }));

  function matches(evt) {
    const prefix = evt.type.split(".")[0];
    if (state.filters.size && !state.filters.has(prefix)) return false;
    if (!state.query) return true;
    return `${evt.type} ${evt.source} ${summary(evt)}`.toLowerCase().includes(state.query);
  }

  function row(evt) {
    const prefix = evt.type.split(".")[0];
    const details = el("pre", { class: "hidden mt-2 overflow-x-auto rounded-lg bg-zinc-950 p-3 font-mono text-xs text-zinc-300",
      text: JSON.stringify({ id: evt.id, priority: evt.priority, payload: evt.payload }, null, 2) });
    return el("div", { class: "py-2" }, [
      el("div", { class: "flex items-center gap-3" }, [
        el("span", { class: "shrink-0 font-mono text-xs tabular-nums text-zinc-500", text: fmt.clock(evt.ts) }),
        el("span", { class: cls("badge", BADGE[prefix] || "badge-zinc"), text: evt.type }),
        el("span", { class: "shrink-0 font-mono text-xs text-zinc-400", text: evt.source }),
        el("span", { class: "min-w-0 flex-1 truncate text-zinc-300", text: summary(evt) }),
        el("button", { class: "btn btn-ghost px-2 py-0.5 text-xs", text: "…", onclick: () => details.classList.toggle("hidden") }),
      ]),
      details,
    ]);
  }

  function renderList() {
    const visible = state.rows.filter(matches);
    list.replaceChildren(...(visible.length ? visible.map(row)
      : [el("div", { class: "py-6 text-center text-zinc-500", text: "Nothing to show." })]));
  }

  function push(evt) {
    state.rows.unshift(evt);
    if (state.rows.length > MAX_ROWS) state.rows.length = MAX_ROWS;
    if (!matches(evt)) return;
    const placeholder = list.firstElementChild;
    if (placeholder && placeholder.textContent === "Nothing to show.") placeholder.remove();
    list.prepend(row(evt));
    while (list.childElementCount > MAX_ROWS) list.lastElementChild.remove();
  }

  const off = socket.on((evt) => {
    if (state.paused) { state.buffered.push(evt); pause.textContent = `Resume (${state.buffered.length})`; return; }
    push(evt);
  });

  root.append(el("section", { class: "card" }, [
    el("div", { class: "mb-4 flex flex-wrap items-center gap-2" }, [...chips, el("span", { class: "flex-1" }), search, pause]),
    list,
  ]));

  async function load() {
    state.rows = await api("api/events?limit=200");
    renderList();
  }
  await load();
  return { unmount: off, refresh: load };
}
