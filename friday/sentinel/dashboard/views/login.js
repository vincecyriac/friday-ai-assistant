import { api } from "../api.js";
import { el } from "../ui.js";

export const title = "Sign in";

export async function mount(root, ctx) {
  const user = el("input", { class: "input", id: "u", autocomplete: "username", autofocus: true });
  const pass = el("input", { class: "input", id: "p", type: "password", autocomplete: "current-password" });
  const error = el("div", { class: "field-error hidden" });
  const button = el("button", { class: "btn btn-primary w-full justify-center", type: "submit", text: "Sign in" });
  const form = el("form", {
    class: "card mx-auto mt-16 w-full max-w-sm space-y-4",
    onsubmit: async (event) => {
      event.preventDefault();
      error.classList.add("hidden");
      button.disabled = true;
      try {
        await api("auth/login", { method: "POST", json: { username: user.value, password: pass.value } });
        await ctx.boot();
      } catch (e) {
        error.textContent = e.status === 429 ? `Too many attempts — retry in ${e.body.retry_after} s` : e.message;
        error.classList.remove("hidden");
      } finally {
        button.disabled = false;
      }
    },
  }, [
    el("div", { class: "flex items-center gap-2 pb-2" }, [
      el("span", { class: "dot bg-zinc-300" }), el("span", { class: "font-semibold tracking-wide", text: "FRIDAY" }),
      el("span", { class: "text-xs text-zinc-400", text: "sentinel" }),
    ]),
    el("div", {}, [el("label", { class: "label", for: "u", text: "Username" }), user]),
    el("div", {}, [el("label", { class: "label", for: "p", text: "Password" }), pass]),
    error,
    button,
  ]);
  root.append(form);
  user.focus();
}
