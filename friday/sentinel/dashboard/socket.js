/* One /ws connection for the session: subscribe to everything, fan events out to
   views, reconnect with backoff, expose a state for the connection chip. */

const listeners = new Set();
const stateListeners = new Set();
let ws = null;
let wanted = false;
let attempts = 0;
let timer = null;
let state = "offline";

function wsUrl() {
  const url = new URL("ws", location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

function setState(next) {
  if (next === state) return;
  state = next;
  stateListeners.forEach((fn) => fn(state));
}

function open() {
  ws = new WebSocket(wsUrl());
  ws.onopen = () => {
    attempts = 0;
    setState("live");
    ws.send(JSON.stringify({ subscribe: ["*"] }));
  };
  ws.onmessage = (message) => {
    let data;
    try { data = JSON.parse(message.data); } catch (e) { return; }
    if (!data || !data.type) return;
    listeners.forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } });
  };
  ws.onclose = () => {
    ws = null;
    if (!wanted) { setState("offline"); return; }
    attempts += 1;
    setState(attempts >= 5 ? "offline" : "reconnecting");
    timer = setTimeout(open, Math.min(30000, 1000 * 2 ** (attempts - 1)));
  };
  ws.onerror = () => {};
}

export const socket = {
  get state() { return state; },
  connect() { if (wanted) return; wanted = true; open(); },
  close() { wanted = false; clearTimeout(timer); if (ws) ws.close(); },
  on(fn) { listeners.add(fn); return () => listeners.delete(fn); },
  onState(fn) { stateListeners.add(fn); fn(state); return () => stateListeners.delete(fn); },
};
