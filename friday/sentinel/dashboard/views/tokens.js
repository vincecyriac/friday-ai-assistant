import { api } from "../api.js";
import { cls, el, fmt, toast } from "../ui.js";

export const title = "Nodes & tokens";

export async function mount(root, ctx) {
  const name = el("input", { class: "input", placeholder: "e.g. desktop, pixel" });
  const reveal = el("div", { class: "hidden space-y-2 pt-2" });
  const create = el("button", {
    class: "btn btn-primary",
    text: "Create token",
    onclick: async () => {
      try {
        const made = await api("api/tokens", { method: "POST", json: { name: name.value } });
        reveal.classList.remove("hidden");
        reveal.replaceChildren(
          el("p", { class: "text-xs text-zinc-400", text: `Token for ${made.name} — shown once. Put it in that node's .env as FRIDAY_SENTINEL_TOKEN.` }),
          el("div", { class: "break-all rounded-lg border border-dashed border-amber-500/60 bg-zinc-950 p-3 font-mono text-xs", text: made.token }),
          el("button", { class: "btn btn-ghost", text: "Copy", onclick: () =>
            navigator.clipboard.writeText(made.token).then(() => toast("Copied")) }));
        name.value = "";
        await renderTokens();
      } catch (e) { toast(e.message, { error: true }); }
    },
  });
  const tokenCard = el("section", { class: "card" }, [el("h2", { class: "card-title", text: "Node tokens" })]);
  const nodeCard = el("section", { class: "card" }, [el("h2", { class: "card-title", text: "Nodes (last heartbeat)" })]);

  function table(headers, rows) {
    return el("div", { class: "overflow-x-auto" }, [el("table", { class: "w-full text-sm" }, [
      el("thead", {}, [el("tr", {}, headers.map((h) => el("th", { class: "th", text: h })))]),
      el("tbody", {}, rows),
    ])]);
  }

  async function renderTokens() {
    const rows = await api("api/tokens");
    const body = rows.map((t) => el("tr", {}, [
      el("td", { class: "td font-mono text-xs", text: t.id }), el("td", { class: "td", text: t.name }),
      el("td", { class: "td text-zinc-400", text: fmt.when(t.created_at) }),
      el("td", { class: "td text-zinc-400", text: fmt.when(t.last_used) }),
      el("td", { class: "td" }, [el("span", { class: cls("badge", t.revoked_at ? "badge-zinc" : "badge-emerald"),
        text: t.revoked_at ? "revoked" : "active" })]),
      el("td", { class: "td text-right" }, [t.revoked_at ? null : el("button", { class: "btn btn-danger px-2 py-1", text: "Revoke",
        onclick: async () => {
          try { await api(`api/tokens/${t.id}`, { method: "DELETE" }); toast("Revoked"); await renderTokens(); }
          catch (e) { toast(e.message, { error: true }); }
        } })]),
    ]));
    tokenCard.replaceChildren(el("h2", { class: "card-title", text: "Node tokens" }),
      rows.length ? table(["ID", "Name", "Created", "Last used", "Status", ""], body)
        : el("p", { class: "text-zinc-400", text: "No tokens yet." }));
  }

  async function renderNodes() {
    const nodes = await api("nodes");
    const body = nodes.map((n) => el("tr", {}, [
      el("td", { class: "td font-mono text-xs", text: n.node_id }), el("td", { class: "td", text: n.status }),
      el("td", { class: "td text-zinc-400", text: fmt.ago(n.last_seen) }),
      el("td", { class: "td text-zinc-400", text: (n.meta && n.meta.platform) || "" }),
    ]));
    nodeCard.replaceChildren(el("h2", { class: "card-title", text: "Nodes (last heartbeat)" }),
      nodes.length ? table(["Node", "Status", "Last seen", "Platform"], body)
        : el("p", { class: "text-zinc-400", text: "No heartbeats yet." }));
  }

  root.append(
    el("div", { class: "grid grid-cols-1 gap-4 xl:grid-cols-2" }, [
      el("section", { class: "card" }, [el("h2", { class: "card-title", text: "New node token" }),
        el("label", { class: "label", text: "Name" }), name,
        el("div", { class: "mt-3 flex justify-end" }, [create]), reveal]),
      nodeCard,
    ]),
    el("div", { class: "mt-4" }, [tokenCard]));
  await Promise.all([renderTokens(), renderNodes()]);
  return { refresh: renderNodes };
}
