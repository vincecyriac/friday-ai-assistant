import { api } from "../api.js";
import { el, sourceBadge, toast } from "../ui.js";

export const title = "Settings";

export async function mount(root, ctx) {
  const [schema, current] = await Promise.all([api("api/settings/schema"), api("api/settings")]);
  const values = Object.fromEntries(current.values.map((v) => [v.key, v]));
  const inputs = {};
  const errors = {};
  const grid = el("div", { class: "grid grid-cols-1 gap-4 xl:grid-cols-2" });

  schema.groups.forEach((group) => {
    const fields = el("div", { class: "space-y-4" });
    group.keys.forEach((spec) => {
      const cur = values[spec.key] || {};
      let input;
      if (spec.type === "enum") {
        input = el("select", { class: "input" }, spec.choices.map((choice) =>
          el("option", { value: choice, selected: choice === cur.value, text: choice })));
      } else if (spec.type === "bool") {
        input = el("select", { class: "input" }, [["true", "on"], ["false", "off"]].map(([value, label]) =>
          el("option", { value, selected: String(cur.value) === value, text: label })));
      } else if (spec.secret) {
        input = el("input", { class: "input", type: "password", autocomplete: "new-password",
          placeholder: cur.set ? `set (…${cur.hint})` : "not set" });
      } else {
        input = el("input", { class: "input", value: cur.value === null || cur.value === undefined ? "" : String(cur.value) });
      }
      input.dataset.original = spec.secret ? "" : input.value;
      inputs[spec.key] = { input, spec };
      errors[spec.key] = el("div", { class: "field-error hidden" });
      fields.append(el("div", {}, [
        el("div", { class: "mb-1.5 flex items-center justify-between gap-2" }, [
          el("label", { class: "font-mono text-xs text-zinc-300", text: spec.key }), sourceBadge(cur.source)]),
        input,
        el("div", { class: "help", text: spec.description }),
        errors[spec.key],
      ]));
    });
    grid.append(el("section", { class: "card" }, [el("h2", { class: "card-title", text: group.name }), fields]));
  });

  const save = el("button", {
    class: "btn btn-primary",
    text: "Save changes",
    onclick: async () => {
      Object.values(errors).forEach((node) => node.classList.add("hidden"));
      const changes = {};
      Object.entries(inputs).forEach(([key, { input, spec }]) => {
        const value = input.value;
        const changed = spec.secret ? value !== "" : value !== input.dataset.original;
        if (changed) changes[key] = spec.type === "bool" ? value === "true" : value;
      });
      if (!Object.keys(changes).length) { toast("Nothing changed"); return; }
      save.disabled = true;
      try {
        await api("api/settings", { method: "PUT", json: changes });
        toast("Saved");
        ctx.navigate("settings");
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
  root.append(grid, el("div", { class: "mt-4 flex justify-end" }, [save]));
}
