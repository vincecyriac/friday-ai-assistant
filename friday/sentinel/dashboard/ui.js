/* DOM and formatting helpers. Class strings stay literal (in class:/cls()) so the
   build test can prove every class exists in the committed tailwind.css. */

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (value === null || value === undefined || value === false) return;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : value);
  });
  (Array.isArray(children) ? children : [children]).forEach((child) => {
    if (child === null || child === undefined || child === false) return;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  });
  return node;
}

export function cls(...tokens) {
  return tokens.filter(Boolean).join(" ");
}

let toastTimer = null;
export function toast(message, { error = false } = {}) {
  const node = document.getElementById("toast");
  if (!node) return;
  node.textContent = message;
  node.className = cls("toast", error && "toast-error");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 3500);
}

export const fmt = {
  bytes(n) {
    if (n === null || n === undefined) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = n;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  },
  percent(x) { return x === null || x === undefined ? "—" : `${Math.round(x)}%`; },
  ago(ts) {
    if (!ts) return "never";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 60) return `${s} s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  },
  clock(ts) {
    const d = new Date(ts * 1000);
    return `${d.toLocaleTimeString([], { hour12: false })}.${String(d.getMilliseconds()).padStart(3, "0")}`;
  },
  when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "—"; },
};

/* Threshold → level. `invert` for "lower is worse" metrics such as battery. */
export function level(value, warn, danger, { invert = false } = {}) {
  if (value === null || value === undefined || Number.isNaN(value)) return "none";
  if (invert) return value <= danger ? "danger" : value <= warn ? "warn" : "ok";
  return value >= danger ? "danger" : value >= warn ? "warn" : "ok";
}

export const LEVEL = {
  ok: { dot: cls("bg-emerald-500"), bar: cls("bg-emerald-500"), text: cls("text-emerald-400"), badge: cls("badge-emerald") },
  warn: { dot: cls("bg-amber-500"), bar: cls("bg-amber-500"), text: cls("text-amber-400"), badge: cls("badge-amber") },
  danger: { dot: cls("bg-rose-500"), bar: cls("bg-rose-500"), text: cls("text-rose-400"), badge: cls("badge-rose") },
  none: { dot: cls("bg-zinc-600"), bar: cls("bg-zinc-700"), text: cls("text-zinc-500"), badge: cls("badge-zinc") },
};

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export function sourceBadge(source) {
  const variant = source === "vault" ? cls("badge-emerald") : source === "env" ? cls("badge-amber")
    : source === "undecryptable" ? cls("badge-rose") : cls("badge-zinc");
  return el("span", { class: cls("badge", variant), text: source || "default" });
}
