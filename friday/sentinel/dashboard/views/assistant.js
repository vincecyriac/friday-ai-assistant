import { api, stream } from "../api.js";
import { render } from "../md.js";
import { cls, el, fmt, toast } from "../ui.js";
import { createVoice } from "./voice.js";

export const title = "Assistant";
const RENDER_EVERY_MS = 80;

function statusTag(status) {
  if (status === "complete") return null;
  const failed = status === "error";
  return el("span", { class: cls("badge", failed ? "badge-rose" : "badge-amber"), text: status });
}

function toolAccordion(steps) {
  if (!steps.length) return null;
  const total = steps.reduce((sum, s) => sum + (s.ms || 0), 0);
  const body = el("div", { class: "hidden mt-2 space-y-2" }, steps.map((s) => el("div", { class: "rounded-lg border border-zinc-800 bg-zinc-950 p-2" }, [
    el("div", { class: "flex items-center justify-between font-mono text-xs text-zinc-300" }, [
      el("span", { text: s.name }), el("span", { class: "text-zinc-500", text: s.ms === undefined ? "" : `${s.ms} ms` })]),
    el("pre", { class: "mt-1 overflow-x-auto font-mono text-xs text-zinc-400", text: JSON.stringify(s.args || {}) }),
    s.output === undefined ? null : el("pre", { class: "mt-1 max-h-48 overflow-auto font-mono text-xs text-zinc-300",
      text: s.output.length > 4096 ? `${s.output.slice(0, 4096)}… (${s.output.length} chars)` : s.output }),
  ])));
  const toggle = el("button", { class: "btn btn-ghost px-2 py-0.5 text-xs", text: `Used ${steps.length} tool${steps.length === 1 ? "" : "s"} · ${total} ms`,
    onclick: () => body.classList.toggle("hidden") });
  return el("div", { class: "mb-2" }, [toggle, body]);
}

function micGlyph(via, tone) {
  return via === "voice" ? el("span", { class: cls("mr-2 text-xs", tone), text: "🎙" }) : null;
}

function assistantBubble(content, steps, status, via) {
  const body = el("div", { class: "prose-friday space-y-2" });
  body.append(render(content));
  return el("div", { class: "mr-auto max-w-[85%] rounded-xl border border-zinc-800 bg-zinc-900 px-4 py-3" }, [
    toolAccordion(steps), micGlyph(via, "text-zinc-500"), body, statusTag(status)]);
}

function userBubble(content, via) {
  const bubble = el("div", { class: "ml-auto max-w-[85%] whitespace-pre-wrap rounded-xl border border-indigo-500/40 bg-indigo-500/10 px-4 py-3", text: content });
  const glyph = micGlyph(via, "text-indigo-300");
  if (glyph) bubble.prepend(glyph);
  return bubble;
}

function systemRow(message) {
  return el("div", { class: "text-center text-xs text-zinc-400", text: message });
}

export async function mount(root, ctx) {
  const state = { conversations: [], currentId: null, controller: null };
  const list = el("div", { class: "space-y-1" });
  const picker = el("select", { class: "input md:hidden", onchange: () => select(picker.value) });
  const thread = el("div", { class: "flex-1 space-y-3 overflow-y-auto pr-1" });
  const input = el("textarea", { class: "input min-h-[2.75rem] resize-none", rows: "1", placeholder: "Ask FRIDAY…",
    onkeydown: (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(); } } });
  const sendButton = el("button", { class: "btn btn-primary", text: "Send", onclick: () => send() });
  const stopButton = el("button", { class: "btn btn-ghost hidden", text: "Stop", onclick: () => state.controller && state.controller.abort() });
  const orbStage = el("div", { class: "h-40 w-full" });
  const caption = el("div", { class: "min-h-[1.25rem] text-center text-xs text-zinc-400" });
  const voice = createVoice({
    orbStage,
    conversationId: () => state.currentId,
    onTranscript: (msg) => { caption.textContent = msg.final ? "" : msg.text; },
    onTool: (msg) => { if (msg.phase === "done") toast(`${msg.name} · ${msg.ms} ms`); },
    onTurnComplete: async () => {
      caption.textContent = "";
      try { await select(state.currentId); } catch (e) { /* signed out or deleted */ }
    },
  });
  const micButton = el("button", {
    class: "btn", text: "Voice",
    onclick: async () => {
      await voice.toggle();
      micButton.className = cls("btn", voice.active && "btn-primary");
      micButton.textContent = voice.active ? "Stop voice" : "Voice";
    },
  });
  if (!voice.supported) {
    micButton.disabled = true;
    micButton.title = "This browser cannot capture audio";
  }
  const newButton = el("button", { class: "btn w-full justify-center", text: "New chat", onclick: async () => {
    const conv = await api("api/chat", { method: "POST", json: {} });
    await loadList();
    await select(conv.id);
  } });

  async function loadList() {
    state.conversations = await api("api/chat");
    list.replaceChildren(...state.conversations.map((c) => {
      const active = c.id === state.currentId;
      return el("div", { class: cls("flex items-center gap-1 rounded-lg px-2 py-1.5", active && "bg-zinc-900") }, [
        el("button", { class: "min-w-0 flex-1 truncate text-left text-sm", text: c.title, title: fmt.when(c.updated_at), onclick: () => select(c.id) }),
        el("button", { class: "btn btn-ghost px-1.5 py-0.5 text-xs", text: "×", title: "Delete", onclick: async () => {
          if (!window.confirm(`Delete "${c.title}"?`)) return;
          await api(`api/chat/${c.id}`, { method: "DELETE" });
          if (state.currentId === c.id) state.currentId = null;
          await loadList();
          if (!state.currentId && state.conversations.length) await select(state.conversations[0].id);
          else if (!state.currentId) thread.replaceChildren(systemRow("Start a new chat."));
        } }),
      ]);
    }));
    picker.replaceChildren(...state.conversations.map((c) => el("option", { value: c.id, selected: c.id === state.currentId, text: c.title })));
  }

  function renderThread(messages) {
    thread.replaceChildren();
    let steps = [];
    messages.forEach((m) => {
      if (m.role === "user") { thread.append(userBubble(m.content, m.via)); return; }
      if (m.role === "tool") { steps.push({ name: m.tool_name, args: m.tool_args, output: m.tool_result }); return; }
      thread.append(assistantBubble(m.content, steps, m.status, m.via));
      steps = [];
    });
    if (steps.length) thread.append(assistantBubble("", steps, "complete"));
    if (!messages.length) thread.append(systemRow("Ask about nodes, telemetry, recent activity — or tell FRIDAY to mute calls."));
    thread.scrollTop = thread.scrollHeight;
  }

  async function select(id) {
    state.currentId = id;
    const body = await api(`api/chat/${id}`);
    renderThread(body.messages);
    await loadList();
  }

  function liveBubble() {
    const dots = el("div", { class: "flex gap-1 motion-safe:animate-pulse text-zinc-500", text: "•••" });
    const body = el("div", { class: "space-y-2" }, [dots]);
    const stepsBox = el("div", {});
    const node = el("div", { class: "mr-auto max-w-[85%] rounded-xl border border-zinc-800 bg-zinc-900 px-4 py-3" }, [stepsBox, body]);
    const steps = [];
    let text = "";
    let timer = null;
    let started = false;
    function paint() {
      timer = null;
      body.replaceChildren();
      body.append(render(text));
      body.append(el("span", { class: "inline-block h-4 w-1.5 motion-safe:animate-pulse bg-indigo-400 align-middle" }));
      thread.scrollTop = thread.scrollHeight;
    }
    return {
      node,
      delta(chunk) {
        text += chunk;
        if (!started) { started = true; dots.remove(); }
        if (!timer) timer = setTimeout(paint, RENDER_EVERY_MS);
      },
      tool(name, args) {
        steps.push({ name, args });
        stepsBox.replaceChildren(toolAccordion(steps) || "");
      },
      result(name, output, ms) {
        const step = [...steps].reverse().find((s) => s.name === name && s.output === undefined);
        if (step) { step.output = output; step.ms = ms; }
        stepsBox.replaceChildren(toolAccordion(steps) || "");
      },
      error(message) { body.append(systemRow(message)); },
    };
  }

  async function send() {
    const content = input.value.trim();
    if (!content || state.controller || !state.currentId) return;
    input.value = "";
    thread.append(userBubble(content));
    const live = liveBubble();
    thread.append(live.node);
    thread.scrollTop = thread.scrollHeight;
    state.controller = new AbortController();
    sendButton.disabled = true;
    input.disabled = true;
    stopButton.classList.remove("hidden");
    try {
      for await (const evt of stream(`api/chat/${state.currentId}/messages`, { method: "POST", json: { content }, signal: state.controller.signal })) {
        if (evt.type === "delta") live.delta(evt.text);
        else if (evt.type === "tool") live.tool(evt.name, evt.args);
        else if (evt.type === "result") live.result(evt.name, evt.output, evt.ms);
        else if (evt.type === "error") live.error(evt.message);
      }
    } catch (e) {
      if (e.name !== "AbortError") { live.error(e.message); toast(e.message, { error: true }); }
    } finally {
      state.controller = null;
      sendButton.disabled = false;
      input.disabled = false;
      stopButton.classList.add("hidden");
      try { await select(state.currentId); } catch (e) { /* signed out or deleted */ }
      input.focus();
    }
  }

  root.append(el("div", { class: "flex h-[calc(100vh-9rem)] gap-4" }, [
    el("aside", { class: "hidden w-64 shrink-0 flex-col gap-3 md:flex" }, [newButton, el("div", { class: "min-h-0 flex-1 overflow-y-auto" }, [list])]),
    el("div", { class: "flex min-w-0 flex-1 flex-col gap-3" }, [
      el("div", { class: "flex gap-2 md:hidden" }, [picker, el("button", { class: "btn", text: "New", onclick: () => newButton.click() })]),
      orbStage,
      caption,
      el("section", { class: "card flex min-h-0 flex-1 flex-col" }, [thread]),
      el("div", { class: "flex items-end gap-2" }, [input, micButton, stopButton, sendButton]),
    ]),
  ]));

  await loadList();
  if (state.conversations.length) await select(state.conversations[0].id);
  else thread.append(systemRow("Start a new chat."));
  return {
    unmount: () => { voice.stop(); if (state.controller) state.controller.abort(); },
    refresh: loadList,
  };
}
