/* FRIDAY sentinel dashboard — vanilla JS, relative URLs only (works behind a path prefix). */
(function () {
  "use strict";
  const main = document.getElementById("main");
  const nav = document.getElementById("nav");
  const toastEl = document.getElementById("toast");
  const state = { user: null, schema: null };

  // ---------------------------------------------------------------- http
  async function api(path, options) {
    const opts = Object.assign({ credentials: "same-origin", headers: {} }, options || {});
    opts.headers = Object.assign({ "X-FRIDAY-Client": "dashboard" }, opts.headers);
    if (opts.json !== undefined) {
      opts.body = JSON.stringify(opts.json);
      opts.headers["Content-Type"] = "application/json";
      delete opts.json;
    }
    const resp = await fetch(path, opts);
    if (resp.status === 401 && path !== "auth/login") {
      state.user = null;
      render("login");
      throw new Error("signed out");
    }
    let body = null;
    const text = await resp.text();
    if (text) { try { body = JSON.parse(text); } catch (e) { body = text; } }
    if (!resp.ok) {
      const err = new Error((body && body.error) || `HTTP ${resp.status}`);
      err.status = resp.status; err.body = body;
      throw err;
    }
    return body;
  }

  function toast(message, isError) {
    toastEl.textContent = message;
    toastEl.className = "toast" + (isError ? " error" : "");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => toastEl.classList.add("hidden"), 3500);
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    });
    (children || []).forEach((c) => node.append(c instanceof Node ? c : document.createTextNode(String(c))));
    return node;
  }

  // --------------------------------------------------------------- views
  function render(view) {
    main.replaceChildren();
    nav.classList.toggle("hidden", !state.user);
    nav.querySelectorAll("a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
    ({ login: renderLogin, settings: renderSettings, tokens: renderTokens })[view]();
  }

  function renderLogin() {
    const user = el("input", { id: "u", autocomplete: "username" });
    const pass = el("input", { id: "p", type: "password", autocomplete: "current-password" });
    const err = el("div", { class: "err" });
    const form = el("form", {
      class: "card login",
      onsubmit: async (e) => {
        e.preventDefault();
        err.textContent = "";
        try {
          await api("auth/login", { method: "POST", json: { username: user.value, password: pass.value } });
          await boot();
        } catch (ex) {
          err.textContent = ex.status === 429 ? `Too many attempts — retry in ${ex.body.retry_after}s` : ex.message;
        }
      },
    }, [el("h2", {}, ["Sign in"]), el("label", {}, ["Username"]), user, el("label", {}, ["Password"]), pass,
        err, el("div", { class: "actions" }, [el("button", { type: "submit" }, ["Sign in"])])]);
    main.append(form);
    user.focus();
  }

  async function renderSettings() {
    const [schema, current] = await Promise.all([api("api/settings/schema"), api("api/settings")]);
    const values = Object.fromEntries(current.values.map((v) => [v.key, v]));
    const inputs = {};
    const errors = {};
    schema.groups.forEach((group) => {
      const card = el("div", { class: "card" }, [el("h2", {}, [group.name])]);
      const grid = el("div", { class: "row" });
      group.keys.forEach((spec) => {
        const cur = values[spec.key] || {};
        let input;
        if (spec.type === "enum") {
          input = el("select", {}, spec.choices.map((c) => el("option", { value: c, selected: c === cur.value ? "" : null }, [c])));
        } else if (spec.type === "bool") {
          input = el("select", {}, [["true", "on"], ["false", "off"]].map(([v, label]) =>
            el("option", { value: v, selected: String(cur.value) === v ? "" : null }, [label])));
        } else if (spec.secret) {
          input = el("input", { type: "password", placeholder: cur.set ? `set (…${cur.hint})` : "not set", autocomplete: "new-password" });
        } else {
          input = el("input", { value: cur.value === null || cur.value === undefined ? "" : cur.value });
        }
        input.dataset.original = spec.secret ? "" : (input.value ?? "");
        inputs[spec.key] = { input, spec };
        errors[spec.key] = el("div", { class: "err" });
        grid.append(el("div", { class: "field" }, [
          el("label", {}, [spec.key, el("span", { class: `badge ${cur.source || "default"}` }, [cur.source || "default"])]),
          input, el("div", { class: "desc" }, [spec.description]), errors[spec.key]]));
      });
      card.append(grid);
      main.append(card);
    });
    const save = el("button", {
      onclick: async () => {
        Object.values(errors).forEach((e) => (e.textContent = ""));
        const changes = {};
        Object.entries(inputs).forEach(([key, { input, spec }]) => {
          const v = input.value;
          if (spec.secret ? v !== "" : v !== input.dataset.original) changes[key] = spec.type === "bool" ? v === "true" : v;
        });
        if (!Object.keys(changes).length) return toast("Nothing changed");
        try {
          await api("api/settings", { method: "PUT", json: changes });
          toast("Saved");
          render("settings");
        } catch (ex) {
          Object.entries((ex.body && ex.body.invalid) || {}).forEach(([k, m]) => { if (errors[k]) errors[k].textContent = m; });
          toast(ex.message, true);
        }
      },
    }, ["Save changes"]);
    main.append(el("div", { class: "actions" }, [save]));
  }

  async function renderTokens() {
    const [tokens, nodes] = await Promise.all([api("api/tokens"), api("nodes")]);
    const name = el("input", { placeholder: "e.g. desktop, pixel" });
    const reveal = el("div", { class: "hidden" });
    const create = el("button", {
      onclick: async () => {
        try {
          const made = await api("api/tokens", { method: "POST", json: { name: name.value } });
          reveal.className = "";
          reveal.replaceChildren(
            el("p", {}, [`Token for ${made.name} — shown once. Put it in that node's .env as FRIDAY_SENTINEL_TOKEN.`]),
            el("div", { class: "token-reveal" }, [made.token]),
            el("div", { class: "actions" }, [el("button", { class: "ghost", onclick: () => navigator.clipboard.writeText(made.token).then(() => toast("Copied")) }, ["Copy"])]));
          name.value = "";
          renderTokenTable();
        } catch (ex) { toast(ex.message, true); }
      },
    }, ["Create token"]);
    main.append(el("div", { class: "card" }, [el("h2", {}, ["New node token"]), el("label", {}, ["Name"]), name,
      el("div", { class: "actions" }, [create]), reveal]));
    const tableCard = el("div", { class: "card" }, [el("h2", {}, ["Node tokens"])]);
    main.append(tableCard);
    function renderTokenTable() {
      api("api/tokens").then((rows) => {
        const table = el("table", {}, [el("tr", {}, ["ID", "Name", "Created", "Last used", "Status", ""].map((h) => el("th", {}, [h])))]);
        rows.forEach((t) => table.append(el("tr", {}, [
          el("td", { class: "mono" }, [t.id]), el("td", {}, [t.name]), el("td", {}, [when(t.created_at)]),
          el("td", {}, [when(t.last_used)]), el("td", {}, [t.revoked_at ? "revoked" : "active"]),
          el("td", {}, [t.revoked_at ? "" : el("button", { class: "danger", onclick: async () => {
            try { await api(`api/tokens/${t.id}`, { method: "DELETE" }); toast("Revoked"); renderTokenTable(); }
            catch (ex) { toast(ex.message, true); }
          } }, ["Revoke"])])])));
        tableCard.replaceChildren(el("h2", {}, ["Node tokens"]), rows.length ? table : el("p", { class: "dim" }, ["No tokens yet."]));
      });
    }
    renderTokenTable();
    const nodeTable = el("table", {}, [el("tr", {}, ["Node", "Status", "Last seen", "Platform"].map((h) => el("th", {}, [h])))]);
    nodes.forEach((n) => nodeTable.append(el("tr", {}, [el("td", { class: "mono" }, [n.node_id]), el("td", {}, [n.status]),
      el("td", {}, [when(n.last_seen)]), el("td", {}, [(n.meta && n.meta.platform) || ""])])));
    main.append(el("div", { class: "card" }, [el("h2", {}, ["Nodes (last heartbeat)"]), nodes.length ? nodeTable : el("p", {}, ["No heartbeats yet."])]));
  }

  function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "—"; }

  // ---------------------------------------------------------------- boot
  nav.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-view]");
    if (a) { e.preventDefault(); history.pushState({}, "", a.getAttribute("href")); render(a.dataset.view); }
  });
  document.getElementById("logout").addEventListener("click", async () => {
    try { await api("auth/logout", { method: "POST" }); } catch (e) { /* already signed out */ }
    state.user = null; history.pushState({}, "", "login"); render("login");
  });
  window.addEventListener("popstate", () => route());

  function route() {
    const leaf = location.pathname.split("/").filter(Boolean).pop() || "";
    render(!state.user ? "login" : (leaf === "tokens" ? "tokens" : "settings"));
  }

  async function boot() {
    try { state.user = await api("auth/me"); } catch (e) { state.user = null; }
    if (state.user && (location.pathname.endsWith("/login") || location.pathname.endsWith("/"))) history.replaceState({}, "", "settings");
    route();
  }
  boot();
})();
