/* The Assistant view: FRIDAY's HUD, not a chat window.
 *
 * The orb is the interface. Speech and replies appear as ambient captions that
 * fade; a ghosted command pill takes typed instructions; a transcript drawer
 * (⌘J) holds what was said when you need to look back. Nothing persists on the
 * surface, which is what keeps it a presence rather than a message feed.
 */
import { api, stream } from "../api.js";
import { cls, el, fmt, toast } from "../ui.js";
import { createVoice } from "./voice.js";

export const title = "Assistant";

// The desktop HUD's dwell rule: long enough to read, never longer than needed.
const CAPTION_MIN_MS = 3200;
const CAPTION_PER_CHAR_MS = 55;
const CAPTION_MAX_MS = 14000;
const DRAWER_TURNS = 40;
const WHO_YOU = cls("text-indigo-300");
const WHO_FRIDAY = cls("text-zinc-400");

export async function mount(root, ctx) {
  const state = { conversationId: null, controller: null, drawerOpen: false, captionTimer: null };

  // ---------------------------------------------------------------- surface
  const orbStage = el("div", { class: "hud-orb" });
  const caption = el("div", { class: "caption" });
  const input = el("input", {
    class: "command-input", type: "text", placeholder: "Speak, or type…",
    autocomplete: "off", spellcheck: "false",
    onkeydown: (event) => {
      if (event.key === "Enter") { event.preventDefault(); submit(); }
      else if (event.key === "Escape") input.blur();
    },
  });
  const sendButton = el("button", { class: "command-send", title: "Send", text: "▸",
                                    onclick: () => submit() });
  const micButton = el("button", { class: "command-mic", title: "Start a voice session (⌘/)",
                                   text: "Voice", onclick: () => toggleVoice() });
  const pill = el("div", { class: "command-pill" }, [input, micButton, sendButton]);
  const drawerBody = el("div", { class: "drawer-body" });
  const drawer = el("section", { class: "drawer" }, [
    el("div", { class: "drawer-head" }, [
      el("span", { class: "card-title mb-0", text: "Transcript" }),
      el("button", { class: "btn btn-ghost px-2 py-0.5 text-xs", text: "Close",
                     onclick: () => toggleDrawer(false) }),
    ]),
    drawerBody,
  ]);
  const drawerToggle = el("button", {
    class: "drawer-toggle", text: "Transcript ⌘J", onclick: () => toggleDrawer(),
  });

  root.append(el("div", { class: "hud" }, [
    el("div", { class: "hud-main" }, [orbStage, caption]),
    el("div", { class: "hud-foot" }, [drawerToggle, pill]),
    drawer,
  ]));

  // ---------------------------------------------------------------- caption
  function showCaption(text, kind = "friday") {
    if (!text) return;
    const fromUser = kind === "user";
    clearTimeout(state.captionTimer);
    caption.textContent = text;
    caption.className = cls("caption", "show", fromUser ? "caption-user" : "caption-friday");
    const dwell = Math.min(CAPTION_MAX_MS, CAPTION_MIN_MS + text.length * CAPTION_PER_CHAR_MS);
    state.captionTimer = setTimeout(() => caption.classList.remove("show"), dwell);
  }

  // ------------------------------------------------------------------ voice
  const voice = createVoice({
    orbStage,
    conversationId: () => state.conversationId,
    onTranscript: (msg) => showCaption(msg.text, msg.role === "user" ? "user" : "friday"),
    onTool: (msg) => {
      if (msg.phase === "done") showCaption(`${msg.name} · ${msg.ms} ms`, "friday");
    },
    onTurnComplete: () => { if (state.drawerOpen) loadDrawer(); },
  });

  async function toggleVoice() {
    await voice.toggle();
    micButton.className = cls("command-mic", voice.active && "command-mic-on");
    micButton.textContent = voice.active ? "Stop" : "Voice";
  }

  // ------------------------------------------------------------------- text
  async function submit() {
    const content = input.value.trim();
    if (!content || state.controller) return;
    input.value = "";
    showCaption(content, "user");

    // A live voice session answers aloud in the same turn; otherwise the text
    // assistant answers here.
    if (voice.active && voice.sendText(content)) return;

    state.controller = new AbortController();
    input.disabled = true;
    voice.setState("thinking");
    let answer = "";
    try {
      for await (const evt of stream(`api/chat/${state.conversationId}/messages`,
                                     { method: "POST", json: { content }, signal: state.controller.signal })) {
        if (evt.type === "delta") { answer += evt.text; showCaption(answer, "friday"); }
        else if (evt.type === "result") showCaption(`${evt.name} · ${evt.ms} ms`, "friday");
        else if (evt.type === "error") showCaption(evt.message, "friday");
      }
    } catch (e) {
      if (e.name !== "AbortError") { showCaption(e.message, "friday"); toast(e.message, { error: true }); }
    } finally {
      state.controller = null;
      input.disabled = false;
      voice.setState(voice.active ? "listening" : "idle");
      if (state.drawerOpen) loadDrawer();
      input.focus();
    }
  }

  // ----------------------------------------------------------------- drawer
  async function loadDrawer() {
    const body = await api(`api/chat/${state.conversationId}`);
    const rows = body.messages.filter((m) => m.role !== "tool").slice(-DRAWER_TURNS);
    drawerBody.replaceChildren(...(rows.length ? rows.map((m) => {
      const mine = m.role === "user";
      return el("div", { class: "drawer-line" }, [
      el("span", { class: cls("drawer-who", mine ? WHO_YOU : WHO_FRIDAY),
                   text: mine ? "you" : "friday" }),
      el("span", { class: "drawer-text", text: m.content }),
      m.via === "voice" ? el("span", { class: "drawer-via", text: "🎙" }) : null,
      el("span", { class: "drawer-time", text: fmt.when(m.ts) }),
      ]);
    }) : [el("p", { class: "text-xs text-zinc-500", text: "Nothing said yet." })]));
    drawerBody.scrollTop = drawerBody.scrollHeight;
  }

  function toggleDrawer(force) {
    state.drawerOpen = force === undefined ? !state.drawerOpen : force;
    drawer.classList.toggle("drawer-open", state.drawerOpen);
    if (state.drawerOpen) loadDrawer().catch(() => {});
  }

  // -------------------------------------------------------------- shortcuts
  function onKey(event) {
    const typing = document.activeElement === input;
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault(); input.focus();
    } else if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "j") {
      event.preventDefault(); toggleDrawer();
    } else if ((event.metaKey || event.ctrlKey) && event.key === "/") {
      event.preventDefault(); toggleVoice();
    } else if (event.key === "/" && !typing) {
      event.preventDefault(); input.focus();
    }
  }
  window.addEventListener("keydown", onKey);

  // -------------------------------------------------------------------- run
  const conversations = await api("api/chat");
  state.conversationId = conversations.length
    ? conversations[0].id
    : (await api("api/chat", { method: "POST", json: {} })).id;
  voice.mountOrb();
  input.focus();

  return {
    unmount: () => {
      window.removeEventListener("keydown", onKey);
      clearTimeout(state.captionTimer);
      voice.stop();
      if (state.controller) state.controller.abort();
    },
    refresh: () => { if (state.drawerOpen) loadDrawer().catch(() => {}); },
  };
}
