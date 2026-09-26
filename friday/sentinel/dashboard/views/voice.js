/* Browser half of the sentinel's voice session.
 *
 * Mic: 16 kHz mono PCM16 in binary frames. Speaker: 24 kHz mono PCM16 scheduled
 * through an AudioContext. Control frames are JSON. The orb is fed state and a
 * live mic level; everything else the page needs comes back through callbacks.
 *
 * Both AudioContexts are created and resumed inside the click that starts a
 * session: browsers only allow audio to begin from a user gesture.
 */
import { toast } from "../ui.js";

const MIC_RATE = 16000;
const SPEAKER_RATE = 24000;
const FRAME_SAMPLES = 2048;

/* An AudioWorklet keeps capture off the main thread; ScriptProcessor is the
   fallback for browsers (and insecure origins) without worklet support. */
const WORKLET_SOURCE = `
class MicCapture extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0][0];
    if (input) {
      const pcm = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        const s = Math.max(-1, Math.min(1, input[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor("mic-capture", MicCapture);
`;

export function createVoice({ orbStage, onTranscript, onTool, onTurnComplete, conversationId }) {
  const state = { ws: null, stream: null, ctx: null, playCtx: null, node: null, analyser: null,
                  orb: null, playAt: 0, sources: new Set(), raf: 0, active: false, workletUrl: null };

  const supported = Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
    && (window.AudioContext || window.webkitAudioContext));

  async function ensureOrb() {
    if (state.orb || !orbStage) return null;
    try {
      const mod = await import("../../shared/orb.js");
      state.orb = mod.mountOrb(orbStage);
      setOrbState(state.active ? "listening" : "idle");
    } catch (e) {
      console.warn("orb unavailable", e);       // WebGL blocked: the rest still works
    }
    return state.orb;
  }

  function setOrbState(name) {
    if (state.orb) state.orb.setState(name);
  }

  function pumpLevel() {
    state.raf = requestAnimationFrame(pumpLevel);
    if (!state.analyser || !state.orb) return;
    const data = new Uint8Array(state.analyser.frequencyBinCount);
    state.analyser.getByteFrequencyData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) sum += data[i];
    state.orb.setLevel(Math.min(1, (sum / data.length) / 96));
  }

  function wsUrl() {
    const url = new URL("voice/ws", location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const id = conversationId && conversationId();
    if (id) url.searchParams.set("conversation", id);
    return url.toString();
  }

  function playChunk(buffer) {
    const ctx = state.playCtx;
    if (!ctx) return;
    const pcm = new Int16Array(buffer);
    if (!pcm.length) return;
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    const audio = ctx.createBuffer(1, f32.length, SPEAKER_RATE);
    audio.getChannelData(0).set(f32);
    const src = ctx.createBufferSource();
    src.buffer = audio;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    state.playAt = Math.max(now + 0.02, state.playAt);
    src.start(state.playAt);
    state.playAt += audio.duration;
    state.sources.add(src);
    src.onended = () => {
      state.sources.delete(src);
      if (!state.sources.size && state.active) setOrbState("listening");
    };
    setOrbState("speaking");
  }

  function clearPlayback() {
    state.sources.forEach((src) => { try { src.stop(); } catch (e) { /* already ended */ } });
    state.sources.clear();
    state.playAt = 0;
    if (state.active) setOrbState("listening");
  }

  function sendPcm(buffer) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(buffer);
  }

  async function startCapture() {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, sampleRate: MIC_RATE, echoCancellation: true,
               noiseSuppression: true, autoGainControl: true },
    });
    const Ctx = window.AudioContext || window.webkitAudioContext;
    state.ctx = new Ctx({ sampleRate: MIC_RATE });
    state.playCtx = new Ctx();
    // Still inside the user gesture: both contexts may start now.
    if (state.ctx.state === "suspended") await state.ctx.resume();
    if (state.playCtx.state === "suspended") await state.playCtx.resume();

    const source = state.ctx.createMediaStreamSource(state.stream);
    state.analyser = state.ctx.createAnalyser();
    state.analyser.fftSize = 64;
    source.connect(state.analyser);

    let usingWorklet = false;
    if (state.ctx.audioWorklet) {
      try {
        state.workletUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "text/javascript" }));
        await state.ctx.audioWorklet.addModule(state.workletUrl);
        const worklet = new AudioWorkletNode(state.ctx, "mic-capture");
        worklet.port.onmessage = (event) => sendPcm(event.data);
        source.connect(worklet);
        state.node = worklet;
        usingWorklet = true;
      } catch (e) {
        console.warn("AudioWorklet unavailable, falling back to ScriptProcessor", e);
      }
    }
    if (!usingWorklet) {
      const processor = state.ctx.createScriptProcessor(FRAME_SAMPLES, 1, 1);
      processor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0);
        const pcm = new Int16Array(input.length);
        for (let i = 0; i < input.length; i++) {
          const s = Math.max(-1, Math.min(1, input[i]));
          pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        sendPcm(pcm.buffer);
      };
      source.connect(processor);
      processor.connect(state.ctx.destination);
      state.node = processor;
    }
    state.raf = requestAnimationFrame(pumpLevel);
  }

  function openSocket() {
    const ws = new WebSocket(wsUrl());
    ws.binaryType = "arraybuffer";
    state.ws = ws;
    ws.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) { playChunk(event.data); return; }
      let msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      if (msg.type === "state") {
        if (msg.value === "reconnecting" || msg.value === "closed") clearPlayback();
        setOrbState(msg.value === "live" ? "listening"
          : msg.value === "reconnecting" ? "thinking" : "offline");
      } else if (msg.type === "clear") clearPlayback();
      else if (msg.type === "transcript" && onTranscript) onTranscript(msg);
      else if (msg.type === "tool" && onTool) onTool(msg);
      else if (msg.type === "turn_complete" && onTurnComplete) onTurnComplete();
      else if (msg.type === "error") { toast(msg.message, { error: true }); stop(); }
    };
    ws.onclose = () => { if (state.active) stop(); };
    ws.onerror = () => { toast("Voice connection failed", { error: true }); };
  }

  async function start() {
    if (state.active) return;
    if (!supported) { toast("This browser cannot capture audio", { error: true }); return; }
    await ensureOrb();
    setOrbState("thinking");
    try {
      await startCapture();
    } catch (e) {
      setOrbState("offline");
      toast(`Microphone unavailable: ${e.message}`, { error: true });
      await teardown();
      return;
    }
    openSocket();
    state.active = true;
  }

  async function teardown() {
    cancelAnimationFrame(state.raf);
    clearPlayback();
    if (state.node) {
      if (state.node.port) state.node.port.onmessage = null;
      else state.node.onaudioprocess = null;
      state.node.disconnect();
      state.node = null;
    }
    if (state.workletUrl) { URL.revokeObjectURL(state.workletUrl); state.workletUrl = null; }
    if (state.stream) { state.stream.getTracks().forEach((t) => t.stop()); state.stream = null; }
    for (const key of ["ctx", "playCtx"]) {
      if (state[key]) { try { await state[key].close(); } catch (e) { /* already closed */ } state[key] = null; }
    }
    state.analyser = null;
  }

  function stop() {
    if (!state.active && !state.ws) return;
    state.active = false;
    if (state.ws) { try { state.ws.close(); } catch (e) { /* already closing */ } state.ws = null; }
    teardown();
    setOrbState("idle");
  }

  return {
    /** Put the orb on screen. Called when the view mounts — FRIDAY's presence is
     *  there before you speak to her, and WebGL needs no user gesture. */
    mountOrb: ensureOrb,
    /** Type into a live voice turn, so FRIDAY answers aloud in the same session. */
    sendText(text) {
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return false;
      state.ws.send(JSON.stringify({ type: "text", content: text }));
      return true;
    },
    /** Drive the orb from outside a voice session (the text path uses this). */
    setState: setOrbState,
    async toggle() { if (state.active) stop(); else await start(); },
    stop,
    get active() { return state.active; },
    get supported() { return supported; },
  };
}
