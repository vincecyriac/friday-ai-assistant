import { api } from "../api.js";
import { socket } from "../socket.js";
import { LEVEL, cls, debounce, el, fmt, level } from "../ui.js";

export const title = "Overview";
const REFRESH_TYPES = new Set(["node.heartbeat", "telemetry.sample", "sentinel.started",
                               "monitor.item", "monitor.error"]);
const WATCH_TONE = { watching: "ok", disabled: "none", unconfigured: "none",
                     degraded: "warn", needs_reauth: "danger" };
const WATCH_LABEL = { watching: "watching", disabled: "off", unconfigured: "not configured",
                      degraded: "degraded", needs_reauth: "needs reauth" };

function tile(label, value, pct, lvl, subtitle) {
  return el("div", { class: "rounded-lg border border-zinc-800 bg-zinc-950 p-3" }, [
    el("div", { class: "flex items-baseline justify-between gap-2" }, [
      el("span", { class: "text-xs text-zinc-400", text: label }),
      el("span", { class: cls("text-lg font-semibold tabular-nums", LEVEL[lvl].text), text: value }),
    ]),
    el("div", { class: "bar mt-2" }, [
      el("div", { class: cls("bar-fill", LEVEL[lvl].bar), style: `width: ${Math.max(0, Math.min(100, pct || 0))}%` })]),
    subtitle ? el("div", { class: "help truncate", text: subtitle }) : null,
  ]);
}

function pct(used, total) {
  return used === null || used === undefined || !total ? null : (used / total) * 100;
}

function metricTiles(s) {
  const tiles = [];
  tiles.push(tile("CPU", fmt.percent(s.cpu_percent), s.cpu_percent, level(s.cpu_percent, 75, 90)));
  const mem = pct(s.mem_used, s.mem_total);
  tiles.push(tile("Memory", fmt.percent(mem), mem, level(mem, 75, 90),
    mem === null ? "" : `${fmt.bytes(s.mem_used)} / ${fmt.bytes(s.mem_total)}`));
  const disk = pct(s.disk_used, s.disk_total);
  tiles.push(tile("Disk", fmt.percent(disk), disk, level(disk, 75, 90),
    disk === null ? "" : `${fmt.bytes(s.disk_used)} / ${fmt.bytes(s.disk_total)} · ${s.disk_path || ""}`));
  const zones = Object.entries(s.thermal || {});
  if (zones.length) {
    const [zone, temp] = zones.reduce((a, b) => (b[1] > a[1] ? b : a));
    tiles.push(tile("Thermal", `${temp.toFixed(0)} °C`, temp, level(temp, 65, 80), zone));
  } else {
    tiles.push(tile("Thermal", "—", 0, "none", "no sensor"));
  }
  const p = s.power || {};
  if (p.battery_percent !== null && p.battery_percent !== undefined) {
    tiles.push(tile("Power", `${Math.round(p.battery_percent)}%${p.on_ac ? " ⚡" : ""}`, p.battery_percent,
      level(p.battery_percent, 30, 15, { invert: true }), p.on_ac ? "on mains" : "on battery"));
  } else if (p.under_voltage) {
    tiles.push(tile("Power", "under-voltage", 100, "danger", `flags 0x${(p.throttled_flags || 0).toString(16)}`));
  } else if (p.throttled_flags) {
    tiles.push(tile("Power", "throttled", 100, "warn", `flags 0x${p.throttled_flags.toString(16)}`));
  } else {
    tiles.push(tile("Power", "mains", 0, "none", ""));
  }
  return tiles;
}

function queueTiles(health) {
  const q = health.queue || {};
  return el("div", { class: "grid grid-cols-4 gap-2" }, ["pending", "processing", "done", "failed"].map((key) => {
    const alarm = key === "failed" && q[key] > 0;
    return el("div", { class: "rounded-lg border border-zinc-800 bg-zinc-950 p-2 text-center" }, [
      el("div", { class: cls("text-lg font-semibold tabular-nums", alarm && "text-rose-400"), text: String(q[key] ?? 0) }),
      el("div", { class: "text-xs text-zinc-500", text: key }),
    ]);
  }));
}

export async function mount(root) {
  const grid = el("div", { class: "grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3" });
  root.append(grid);
  const data = { nodes: [], telemetry: {}, health: null, heartbeat: 30, watch: null };

  async function load() {
    const [nodes, telemetry, health, settings, watch] = await Promise.all([
      api("nodes"), api("api/telemetry"), api("health"), api("api/settings"), api("api/watch")]);
    data.nodes = nodes;
    data.watch = watch;
    data.telemetry = telemetry.nodes || {};
    data.health = health;
    const hb = settings.values.find((v) => v.key === "sentinel.heartbeat_interval_s");
    if (hb && hb.value) data.heartbeat = Number(hb.value);
    draw();
  }

  function nodeCard(node) {
    const age = Date.now() / 1000 - node.last_seen;
    const lvl = age <= 2 * data.heartbeat ? "ok" : age <= 5 * data.heartbeat ? "warn" : "danger";
    const isSentinel = data.health && node.node_id === data.health.node_id;
    const snapshot = data.telemetry[node.node_id];
    const children = [
      el("div", { class: "flex items-center justify-between gap-2" }, [
        el("span", { class: "truncate font-mono text-sm", text: node.node_id }),
        el("span", { class: cls("badge", LEVEL[lvl].badge) }, [
          el("span", { class: cls("dot", LEVEL[lvl].dot) }), node.status]),
      ]),
      el("div", { class: "mt-1 text-xs text-zinc-400", text:
        `v${(node.meta && node.meta.version) || "?"} · ${(node.meta && node.meta.platform) || "?"} · last seen ${fmt.ago(node.last_seen)}` }),
    ];
    if (isSentinel) {
      children.push(el("div", { class: "mt-3 text-xs text-zinc-400", text: `queue · uptime ${Math.round(data.health.uptime_s / 60)} min` }));
      children.push(el("div", { class: "mt-1" }, [queueTiles(data.health)]));
    }
    if (snapshot) {
      children.push(el("div", { class: "mt-3 grid grid-cols-2 gap-2" }, metricTiles(snapshot)));
    } else {
      children.push(el("div", { class: "help mt-3", text: "no telemetry yet" }));
    }
    return el("section", { class: "card" }, children);
  }

  function watchCard(watch) {
    const names = Object.keys(watch.sources || {});
    const rows = names.map((name) => {
      const info = watch.sources[name];
      const tone = WATCH_TONE[info.state] || "none";
      return el("div", { class: "flex items-baseline justify-between gap-3 py-1.5" }, [
        el("span", { class: "flex items-center gap-2" }, [
          el("span", { class: cls("dot", LEVEL[tone].dot) }),
          el("span", { class: "font-mono text-xs", text: name }),
          el("span", { class: cls("text-xs", LEVEL[tone].text),
                       text: WATCH_LABEL[info.state] || info.state }),
        ]),
        el("span", { class: "min-w-0 truncate text-right text-xs text-zinc-500",
                     title: info.last_error || "",
                     text: info.last_error
                       ? info.last_error
                       : `${info.items_today} today · ${fmt.ago(info.last_poll)}` }),
      ]);
    });
    const recent = (watch.recent || []).slice(0, 5).map((r) => el("div", {
      class: "truncate py-0.5 text-xs text-zinc-400",
      text: `${r.source} · ${r.title}`, title: `${r.who} — ${fmt.when(r.first_seen)}` }));
    return el("section", { class: "card" }, [
      el("h2", { class: "card-title", text: "Watching" }),
      names.length ? el("div", { class: "divide-y divide-zinc-800/60" }, rows)
                   : el("p", { class: "text-zinc-400", text: "No sources configured yet." }),
      recent.length ? el("div", { class: "mt-3 border-t border-zinc-800 pt-2" }, recent) : null,
    ]);
  }

  function draw() {
    const cards = data.nodes.length ? data.nodes.map(nodeCard)
      : [el("div", { class: "card text-zinc-400", text: "No nodes have reported yet." })];
    if (data.watch && Object.keys(data.watch.sources || {}).length) cards.unshift(watchCard(data.watch));
    grid.replaceChildren(...cards);
  }

  const reload = debounce(() => load().catch(() => {}), 500);
  const off = socket.on((evt) => { if (REFRESH_TYPES.has(evt.type)) reload(); });
  const ticker = setInterval(draw, 5000);
  const poll = setInterval(() => load().catch(() => {}), 30000);
  await load();
  return {
    refresh: () => load().catch(() => {}),
    unmount: () => { off(); clearInterval(ticker); clearInterval(poll); },
  };
}
