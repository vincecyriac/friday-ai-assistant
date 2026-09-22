import { api } from "../api.js";
import { socket } from "../socket.js";
import { cls, el, sourceBadge, toast } from "../ui.js";

export const title = "Controls";
const CALL_MODES = [["always", "Always call"], ["urgent_only", "Urgent only"], ["mute", "Mute"]];
const MONITORS = [
  ["controls.monitors.email", "Email", "Read the mailbox and draft replies; never sends or deletes."],
  ["controls.monitors.calendar", "Calendar", "Read events, create reminders, guard meetings."],
  ["controls.monitors.jira", "Jira", "Read-only watch for blockers on your tickets."],
];
const TIMING = [
  ["sentinel.telemetry_interval_s", "Telemetry interval", "seconds"],
  ["sentinel.heartbeat_interval_s", "Heartbeat interval", "seconds"],
  ["sentinel.retention_days", "Event & telemetry retention", "days"],
  ["sentinel.chat_retention_days", "Chat retention", "days"],
];

export async function mount(root) {
  let values = {};

  async function load() {
    const body = await api("api/settings");
    values = Object.fromEntries(body.values.map((v) => [v.key, v]));
  }

  /* Optimistic write of one key: the control already shows the new value; revert on failure. */
  async function put(key, value, revert) {
    try {
      await api("api/settings", { method: "PUT", json: { [key]: value } });
      values[key].value = value;
      values[key].source = "vault";
      toast("Saved");
    } catch (e) {
      revert();
      const detail = e.body && e.body.invalid && e.body.invalid[key];
      toast(detail || e.message, { error: true });
    }
  }

  function segmented(key, options) {
    const buttons = options.map(([value, label]) => el("button", {
      class: cls("segment", values[key].value === value && "segment-active"), text: label,
      onclick: async () => {
        const previous = values[key].value;
        if (previous === value) return;
        set(value);
        buttons.forEach((b) => { b.disabled = true; });
        await put(key, value, () => set(previous));
        buttons.forEach((b) => { b.disabled = false; });
      },
    }));
    function set(value) {
      buttons.forEach((b, i) => b.classList.toggle("segment-active", options[i][0] === value));
      values[key].value = value;
    }
    return el("div", { class: "inline-flex rounded-lg border border-zinc-700 bg-zinc-800 p-1" }, buttons);
  }

  function toggle(key) {
    const knob = el("span", { class: "switch-knob" });
    const button = el("button", {
      class: cls("switch", values[key].value === true && "switch-on"), role: "switch",
      "aria-checked": String(values[key].value === true),
      onclick: async () => {
        const previous = values[key].value === true;
        set(!previous);
        button.disabled = true;
        await put(key, !previous, () => set(previous));
        button.disabled = false;
      },
    }, [knob]);
    function set(on) {
      button.classList.toggle("switch-on", on);
      button.setAttribute("aria-checked", String(on));
      values[key].value = on;
    }
    return button;
  }

  function row(label, description, control) {
    return el("div", { class: "flex items-start justify-between gap-4 py-3" }, [
      el("div", { class: "min-w-0" }, [
        el("div", { class: "text-sm text-zinc-100", text: label }),
        el("div", { class: "help", text: description })]),
      control,
    ]);
  }

  const callsCard = el("section", { class: "card" });
  const monitorsCard = el("section", { class: "card" });
  const timingCard = el("section", { class: "card" });
  const inputs = {};
  const errors = {};

  function drawControls() {
    callsCard.replaceChildren(
      el("h2", { class: "card-title", text: "Calls" }),
      el("div", { class: "divide-y divide-zinc-800/60" }, [
        row("Call mode", "When an escalation may ring the phone.", segmented("controls.call_mode", CALL_MODES)),
        row("Do not disturb", "Suppresses calls and pings; escalations still go to the digest.", toggle("controls.dnd")),
      ]));
    monitorsCard.replaceChildren(
      el("h2", { class: "card-title", text: "Monitors" }),
      el("div", { class: "divide-y divide-zinc-800/60" }, MONITORS.map(([key, label, description]) => row(label, description, toggle(key)))),
      el("p", { class: "help mt-3", text: "Monitors arrive in a later release — the switch is stored now and honoured when they do." }));
  }

  function drawTiming() {
    const fields = TIMING.map(([key, label, unit]) => {
      const input = inputs[key] || el("input", { class: "input", type: "number", step: "any", min: "1" });
      if (document.activeElement !== input) input.value = String(values[key].value ?? "");
      input.dataset.original = String(values[key].value ?? "");
      inputs[key] = input;
      errors[key] = errors[key] || el("div", { class: "field-error hidden" });
      return el("div", {}, [
        el("div", { class: "mb-1.5 flex items-center justify-between" }, [
          el("label", { class: "text-xs text-zinc-300", text: `${label} (${unit})` }), sourceBadge(values[key].source)]),
        input, errors[key],
      ]);
    });
    const save = el("button", {
      class: "btn btn-primary", text: "Save timing",
      onclick: async () => {
        Object.values(errors).forEach((node) => node.classList.add("hidden"));
        const changes = {};
        TIMING.forEach(([key]) => { if (inputs[key].value !== inputs[key].dataset.original) changes[key] = inputs[key].value; });
        if (!Object.keys(changes).length) { toast("Nothing changed"); return; }
        save.disabled = true;
        try {
          await api("api/settings", { method: "PUT", json: changes });
          toast("Saved");
          await load();
          drawTiming();
        } catch (e) {
          Object.entries((e.body && e.body.invalid) || {}).forEach(([key, message]) => {
            if (errors[key]) { errors[key].textContent = message; errors[key].classList.remove("hidden"); }
          });
          toast(e.message, { error: true });
        } finally {
          save.disabled = false;
        }
      },
    });
    timingCard.replaceChildren(
      el("h2", { class: "card-title", text: "Timing" }),
      el("div", { class: "grid grid-cols-1 gap-4 md:grid-cols-2" }, fields),
      el("div", { class: "mt-4 flex justify-end" }, [save]));
  }

  async function refresh() {
    await load();
    drawControls();
    drawTiming();
  }

  root.append(el("div", { class: "grid grid-cols-1 gap-4 xl:grid-cols-2" }, [callsCard, monitorsCard]),
    el("div", { class: "mt-4" }, [timingCard]));
  await refresh();
  const off = socket.on((evt) => { if (evt.type === "config.changed") refresh().catch(() => {}); });
  return { unmount: off, refresh };
}
