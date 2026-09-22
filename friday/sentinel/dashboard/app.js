/* Boot, auth state, router, sidebar/tabs, socket lifecycle. Views live in ./views/. */
import { api } from "./api.js";
import { socket } from "./socket.js";
import { cls, el, toast } from "./ui.js";
import * as activity from "./views/activity.js";
import * as assistant from "./views/assistant.js";
import * as controls from "./views/controls.js";
import * as login from "./views/login.js";
import * as overview from "./views/overview.js";
import * as settings from "./views/settings.js";
import * as tokens from "./views/tokens.js";

const VIEWS = {
  login,
  overview,
  activity,
  controls,
  assistant,
  settings,
  tokens,
};
const NAV = [
  ["overview", "Overview"], ["activity", "Activity"], ["controls", "Controls"],
  ["assistant", "Assistant"], ["settings", "Settings"], ["tokens", "Nodes & tokens"],
];

const root = document.getElementById("view");
const state = { user: null, health: null };
let current = { name: null, handle: null };

function leaf() {
  return location.pathname.split("/").filter(Boolean).pop() || "";
}

function routeName() {
  const name = leaf();
  return VIEWS[name] && name !== "login" ? name : "overview";
}

function navigate(name) {
  history.pushState({}, "", name);
  route();
}

const ctx = { state, navigate, boot, get user() { return state.user; } };

async function render(name) {
  if (current.handle && typeof current.handle.unmount === "function") {
    try { current.handle.unmount(); } catch (e) { console.error(e); }
  }
  current = { name, handle: null };
  root.replaceChildren();
  document.getElementById("view-title").textContent = VIEWS[name].title;
  document.querySelectorAll("[data-view]").forEach((node) => {
    const active = node.dataset.view === name;
    node.classList.toggle("nav-link-active", active && node.classList.contains("nav-link"));
    node.classList.toggle("tab-active", active && node.classList.contains("tab"));
  });
  try {
    current.handle = (await VIEWS[name].mount(root, ctx)) || null;
  } catch (e) {
    if (e.status !== 401) {
      console.error(e);
      root.append(el("div", { class: "card text-rose-300", text: `Could not load ${name}: ${e.message}` }));
    }
  }
}

function route() {
  render(state.user ? routeName() : "login");
}

function setAuthed(authed) {
  const sidebar = document.getElementById("sidebar");
  const tabs = document.getElementById("tabs");
  sidebar.classList.toggle("hidden", !authed);
  sidebar.classList.toggle("md:flex", authed);
  tabs.classList.toggle("hidden", !authed);
  tabs.classList.toggle("flex", authed);
  document.getElementById("user-name").textContent = authed ? state.user.username : "";
}

function buildNav() {
  const nav = document.getElementById("nav");
  const tabs = document.getElementById("tabs");
  nav.replaceChildren();
  tabs.replaceChildren();
  NAV.forEach(([name, label]) => {
    const go = (event) => { event.preventDefault(); navigate(name); };
    nav.append(el("a", { class: "nav-link", href: name, "data-view": name, onclick: go, text: label }));
    tabs.append(el("a", { class: "tab", href: name, "data-view": name, onclick: go, text: label }));
  });
}

const CONN_TONE = { live: cls("bg-emerald-500"), reconnecting: cls("bg-amber-500"), offline: cls("bg-rose-500") };

function bindConnection() {
  const dot = document.getElementById("conn-dot");
  const text = document.getElementById("conn-text");
  let previous = socket.state;
  socket.onState((s) => {
    text.textContent = s;
    dot.className = cls("dot", CONN_TONE[s]);
    if (s === "live" && previous !== "live" && current.handle && typeof current.handle.refresh === "function") {
      current.handle.refresh();
    }
    previous = s;
  });
}

async function loadHealth() {
  try {
    state.health = await api("health");
    document.getElementById("version").textContent = `v${state.health.version}`;
    document.getElementById("brand-node").textContent = state.health.node_id;
  } catch (e) { /* health is decorative */ }
}

async function boot() {
  try { state.user = await api("auth/me"); } catch (e) { state.user = null; }
  setAuthed(Boolean(state.user));
  if (state.user) {
    socket.connect();
    loadHealth();
    if (leaf() === "login" || leaf() === "") history.replaceState({}, "", "overview");
  } else {
    socket.close();
  }
  route();
}

document.getElementById("logout").addEventListener("click", async () => {
  try { await api("auth/logout", { method: "POST" }); } catch (e) { /* already signed out */ }
  state.user = null;
  socket.close();
  setAuthed(false);
  history.pushState({}, "", "login");
  route();
});
window.addEventListener("popstate", route);
window.addEventListener("friday:unauthorized", () => {
  if (!state.user) return;
  state.user = null;
  socket.close();
  setAuthed(false);
  toast("Signed out", { error: true });
  route();
});

buildNav();
bindConnection();
boot();
