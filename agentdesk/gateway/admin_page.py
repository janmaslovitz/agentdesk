"""HTML of the admin cabinet and the /account page: plain HTML + JS, the dashboard's colours (light / dark by the
browser's setting), a single column that also works on a phone. Data is only ever put into the page with textContent."""

STYLE = """
:root{--bg:#f5f6f8;--panel:#fff;--text:#1d2330;--muted:#6b7280;--line:#e3e6eb;--accent:#2f6fde;--ok:#1f9d55;--warn:#c27c0e;
--bad:#d64545;--chip:#eef1f6;--mono:ui-monospace,SFMono-Regular,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#13161b;--panel:#1b1f26;--text:#e6e8ec;--muted:#9aa3af;--line:#2b313b;
--accent:#5b8ef0;--ok:#3ec17a;--warn:#e0a53a;--bad:#ef6b6b;--chip:#252a33}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,"Segoe UI",sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;padding:10px 16px;background:var(--panel);
border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
header h1{font-size:16px;margin:0;flex:1 1 auto}
header a{color:var(--accent);text-decoration:none}
nav{display:flex;flex-wrap:wrap;gap:6px;padding:10px 16px 0}
nav button{background:var(--chip);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 12px;cursor:pointer;font:inherit}
nav button.on{background:var(--accent);border-color:var(--accent);color:#fff}
main{padding:12px 16px 40px;max-width:1100px;margin:0 auto}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 14px}
h2{font-size:14px;margin:0 0 10px}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px}
td.wrap{white-space:normal;overflow-wrap:anywhere}
td.num,th.num{text-align:right}
td.act{display:flex;flex-wrap:wrap;gap:4px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;margin:6px 0}
.row label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted);flex:1 1 150px}
.row label.chk{flex-direction:row;align-items:center;gap:6px;flex:0 0 auto;color:var(--text)}
input,select{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);min-width:0}
button.b{font:inherit;padding:6px 11px;border:1px solid var(--line);border-radius:8px;background:var(--chip);color:var(--text);cursor:pointer}
button.b.p{background:var(--accent);border-color:var(--accent);color:#fff}
button.b.d{color:var(--bad);border-color:var(--bad);background:transparent}
button.b:disabled{opacity:.5;cursor:default}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;background:var(--chip);font-size:12px}
.chip.ok{color:var(--ok)}.chip.warn{color:var(--warn)}.chip.bad{color:var(--bad)}
.muted{color:var(--muted)}
pre{background:var(--bg);border-radius:8px;padding:8px 10px;margin:8px 0 0;max-height:320px;overflow:auto;white-space:pre-wrap;
overflow-wrap:anywhere;font:12px/1.4 var(--mono)}
#msg{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);max-width:92vw;padding:8px 14px;border-radius:8px;
background:var(--panel);border:1px solid var(--line);box-shadow:0 2px 12px rgba(0,0,0,.2);display:none;z-index:9}
#msg.bad{border-color:var(--bad);color:var(--bad)}
.hide{display:none}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--text);max-width:min(420px,92vw)}
dialog::backdrop{background:rgba(0,0,0,.4)}
.stats{display:flex;flex-wrap:wrap;gap:8px 22px}
.stats div span{display:block;color:var(--muted);font-size:12px}
"""

COMMON_JS = """
const CSRF = "__CSRF__";
const $ = (id) => document.getElementById(id);
function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) { if (k === "class") e.className = v; else if (k.startsWith("on")) e[k] = v; else e.setAttribute(k, v); }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}
let msgTimer;
function say(text, bad) { const m = $("msg"); m.textContent = text; m.className = bad ? "bad" : ""; m.style.display = "block"; clearTimeout(msgTimer); msgTimer = setTimeout(() => m.style.display = "none", bad ? 8000 : 3500); }
async function api(method, url, body, raw) {
  const h = { "X-AgentDesk-CSRF": CSRF };
  let data = body;
  if (!raw && body !== undefined) { h["Content-Type"] = "application/json"; data = JSON.stringify(body); }
  const r = await fetch(url, { method, headers: h, body: data, credentials: "same-origin" });
  let j = {};
  try { j = await r.json(); } catch (e) {}
  if (r.status === 401 && j.login) { location.href = j.login; throw new Error("signed out"); }
  if (!r.ok) throw new Error(j.error || ("error " + r.status));
  return j;
}
async function act(fn, ok) { try { const r = await fn(); if (ok) say(ok); return r; } catch (e) { say(e.message, true); } }
function bytes(n) { if (n == null) return ""; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; n = Number(n); while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + " " + u[i]; }
function dur(s) { if (s == null) return ""; s = Math.floor(s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? d + "d " + h + "h" : h ? h + "h " + m + "m" : m ? m + "m" : s + "s"; }
"""

ADMIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin - AgentDesk</title><style>""" + STYLE + """</style></head><body>
<header><h1>AgentDesk Admin</h1><span class="muted">__USER__</span><a href="/">Dashboard</a><a href="/account">Account</a></header>
<nav id="tabs"></nav>
<main>
<div id="p-users">
 <section><h2>New user</h2><div class="row">
  <label>User name<input id="nu" autocomplete="off" autocapitalize="none"></label>
  <label>Role<select id="nr"><option>user</option><option>admin</option></select></label>
  <label>Initial password<input id="np" type="password" autocomplete="new-password"></label>
  <button class="b p" id="ncreate">Create</button></div></section>
 <section><h2>Users</h2><div class="tw"><table><thead><tr><th>User</th><th>Role</th><th>Status</th><th>Claude accounts</th><th>Actions</th></tr></thead>
 <tbody id="ut"></tbody></table></div></section>
</div>
<div id="p-projects" class="hide">
 <section><h2>Projects of a user</h2><div class="row"><label>User<select id="pu"></select></label>
  <button class="b" id="prefresh">Refresh</button></div>
  <div class="tw"><table><thead><tr><th>Project</th><th>Repo</th><th class="num">Size</th><th></th></tr></thead><tbody id="pt"></tbody></table></div></section>
 <section><h2>Add from a git URL (cloned on the server)</h2><div class="row">
  <label style="flex:3 1 260px">Git URL<input id="gu" placeholder="https://host/org/repo.git"></label>
  <label>Project name (optional)<input id="gn"></label><button class="b p" id="gadd">Clone and add</button></div></section>
 <section><h2>Add from an exported .zip</h2><div class="row">
  <label style="flex:3 1 260px">File<input id="zf" type="file" accept=".zip"></label>
  <label>Project name (optional)<input id="zn"></label><button class="b p" id="zadd">Upload and add</button></div></section>
</div>
<div id="p-accounts" class="hide">
 <section><h2>Claude accounts</h2><p class="muted">Keys are never stored or shown here: an API-key account names the environment
 variable (set where the instances run) that holds the key. Users only see the names of the accounts assigned to them.</p>
 <div class="tw"><table><thead><tr><th>Name</th><th>Kind</th><th>Key variable / config folder</th><th></th></tr></thead><tbody id="at"></tbody></table></div></section>
 <section><h2>Add or change an account</h2><div class="row">
  <label>Name<input id="an"></label>
  <label>Kind<select id="ak"><option value="api_key">API key (env variable)</option><option value="subscription">Login (config folder)</option></select></label>
  <label id="aelab">Key variable name<input id="ae" placeholder="ANTHROPIC_API_KEY_TEAM"></label>
  <label id="adlab" class="hide">Config folder (empty = default login)<input id="ad"></label>
  <button class="b p" id="asave">Save</button></div></section>
</div>
<div id="p-instances" class="hide">
 <section><h2>Server</h2><div class="stats" id="health"></div></section>
 <section><h2>Instances</h2><div class="row"><button class="b" id="irefresh">Refresh</button></div>
 <div class="tw"><table><thead><tr><th>User</th><th>State</th><th class="num">Port</th><th class="num">Uptime</th><th class="num">Runs</th><th>Actions</th></tr></thead>
 <tbody id="it"></tbody></table></div><pre id="ilog" class="hide"></pre></section>
</div>
<div id="p-audit" class="hide">
 <section><h2>Audit log</h2><div class="row"><label style="flex:0 0 110px">Last<select id="an2"><option>100</option><option selected>200</option><option>500</option><option>2000</option></select></label>
 <button class="b" id="auref">Refresh</button></div>
 <div class="tw"><table><thead><tr><th>Time</th><th>Event</th><th>User</th><th>By / IP</th><th>Details</th></tr></thead><tbody id="aut"></tbody></table></div></section>
</div>
</main>
<dialog id="dlg"><form method="dialog"><h2 id="dt"></h2><div id="db"></div>
<div class="row"><button class="b" value="no">Cancel</button><button class="b p" id="dok" value="ok">OK</button></div></form></dialog>
<div id="msg" role="status"></div>
<script>""" + COMMON_JS + """
const TABS = [["users", "Users"], ["projects", "Projects"], ["accounts", "Claude accounts"], ["instances", "Instances"], ["audit", "Audit log"]];
let state = { users: [], accounts: [], me: "" };
function show(t) {
  for (const [k] of TABS) { $("p-" + k).classList.toggle("hide", k !== t); }
  for (const b of $("tabs").children) b.classList.toggle("on", b.dataset.t === t);
  history.replaceState(null, "", "#" + t);
  ({ users: loadUsers, projects: loadProjects, accounts: loadAccounts, instances: loadInstances, audit: loadAudit })[t]();
}
for (const [k, n] of TABS) $("tabs").append(el("button", { "data-t": k, onclick: () => show(k) }, n));

function ask(title, body, okText, danger) {
  return new Promise((res) => {
    $("dt").textContent = title; $("db").replaceChildren(...[].concat(body)); $("dok").textContent = okText || "OK";
    $("dok").className = "b " + (danger ? "d" : "p");
    const d = $("dlg"); d.onclose = () => res(d.returnValue === "ok"); d.returnValue = ""; d.showModal();
  });
}
async function loadState() { const j = await api("GET", "/admin/api/users"); state = j; return j; }
async function loadUsers() {
  await act(loadState); const tb = $("ut"); tb.replaceChildren();
  for (const u of state.users) {
    const acc = el("td", { class: "wrap" }, u.accounts.join(", ") || "-");
    const b = (text, fn, cls) => el("button", { class: "b " + (cls || ""), onclick: fn }, text);
    tb.append(el("tr", {}, el("td", {}, u.username, u.username === state.me ? el("span", { class: "muted" }, " (you)") : ""),
      el("td", {}, u.role), el("td", {}, el("span", { class: "chip " + (u.disabled ? "bad" : "ok") }, u.disabled ? "disabled" : "active"), u.running ? el("span", { class: "chip" }, "instance up") : ""),
      acc, el("td", { class: "act" },
        b("Reset password", () => resetPw(u)), b(u.disabled ? "Enable" : "Disable", () => toggle(u)),
        b("Accounts", () => assign(u)), b(u.role === "admin" ? "Make user" : "Make admin", () => role(u)), b("Delete", () => del(u), "d"))));
  }
}
$("ncreate").onclick = () => act(async () => { await api("POST", "/admin/api/users", { username: $("nu").value, role: $("nr").value, password: $("np").value }); $("nu").value = $("np").value = ""; loadUsers(); }, "User created");
async function resetPw(u) {
  const i = el("input", { type: "password", autocomplete: "new-password", placeholder: "New password" });
  if (await ask("Reset password of " + u.username, [i, el("p", { class: "muted" }, "Their sessions end at once.")], "Reset"))
    act(() => api("POST", "/admin/api/users/" + u.username + "/password", { password: i.value }), "Password changed");
}
const toggle = (u) => act(async () => { await api("POST", "/admin/api/users/" + u.username + "/" + (u.disabled ? "enable" : "disable"), {}); loadUsers(); });
const role = (u) => act(async () => { await api("POST", "/admin/api/users/" + u.username + "/role", { role: u.role === "admin" ? "user" : "admin" }); loadUsers(); });
async function assign(u) {
  const boxes = state.accounts.map((a) => { const c = el("input", { type: "checkbox", value: a.name }); c.checked = u.accounts.includes(a.name); return [c, a]; });
  const body = boxes.length ? boxes.map(([c, a]) => el("label", { class: "chk row" }, c, " " + a.name + " (" + a.kind + ")")) : [el("p", { class: "muted" }, "No accounts defined yet (Claude accounts tab).")];
  if (await ask("Claude accounts of " + u.username, body, "Save"))
    act(async () => { await api("POST", "/admin/api/users/" + u.username + "/accounts", { accounts: boxes.filter(([c]) => c.checked).map(([c]) => c.value).join(",") }); loadUsers(); }, "Saved");
}
async function del(u) {
  const t = el("input", { placeholder: u.username, autocomplete: "off" }), c = el("input", { type: "checkbox" });
  const body = [el("p", {}, "Delete the account ", el("b", {}, u.username), "? Type the name to confirm."), t,
    el("label", { class: "chk row" }, c, " Also delete the user's data folder (projects, chats, everything). Unchecked: the folder is kept.")];
  if (await ask("Delete user", body, "Delete", true))
    act(async () => { await api("POST", "/admin/api/users/" + u.username + "/delete", { confirm: t.value, delete_data: c.checked ? "1" : "" }); loadUsers(); }, "User deleted");
}

async function loadProjects() {
  await act(loadState); const sel = $("pu"), cur = sel.value; sel.replaceChildren();
  for (const u of state.users) sel.append(el("option", {}, u.username));
  if (cur) sel.value = cur;
  listProjects();
}
async function listProjects() {
  const tb = $("pt"); tb.replaceChildren(); if (!$("pu").value) return;
  const r = await act(() => api("GET", "/admin/api/users/" + $("pu").value + "/projects")); if (!r) return;
  if (!r.projects.length) tb.append(el("tr", {}, el("td", { class: "muted", colspan: 4 }, "No projects.")));
  for (const p of r.projects) tb.append(el("tr", {}, el("td", {}, p.name, el("span", { class: "muted" }, " " + p.slug)), el("td", { class: "wrap" }, p.repo),
    el("td", { class: "num" }, bytes(p.size + p.repo_size)), el("td", {}, el("button", { class: "b d", onclick: () => rmProject(p) }, "Remove"))));
}
$("pu").onchange = $("prefresh").onclick = listProjects;
async function rmProject(p) {
  const c = el("input", { type: "checkbox" });
  if (await ask("Remove project " + p.name, [el("p", {}, "Removes the project's AgentDesk data (tasks, notes, reports). The code is kept unless you tick:"),
    el("label", { class: "chk row" }, c, " Also delete the repository folder if the server cloned it")], "Remove", true))
    act(async () => { await api("POST", "/admin/api/users/" + $("pu").value + "/projects/" + p.slug + "/remove", { delete_repo: c.checked ? "1" : "" }); listProjects(); }, "Project removed");
}
$("gadd").onclick = async () => { const b = $("gadd"); b.disabled = true; say("Cloning..."); await act(async () => { await api("POST", "/admin/api/users/" + $("pu").value + "/projects/git", { url: $("gu").value, name: $("gn").value }); $("gu").value = ""; listProjects(); }, "Project added"); b.disabled = false; };
$("zadd").onclick = async () => {
  const f = $("zf").files[0]; if (!f) return say("Pick a .zip file first", true);
  const b = $("zadd"); b.disabled = true;
  await act(async () => { await api("POST", "/admin/api/users/" + $("pu").value + "/projects/zip?name=" + encodeURIComponent($("zn").value), f, true); $("zf").value = ""; listProjects(); }, "Project added");
  b.disabled = false;
};

async function loadAccounts() {
  const r = await act(() => api("GET", "/admin/api/accounts")); if (!r) return; const tb = $("at"); tb.replaceChildren();
  if (!r.accounts.length) tb.append(el("tr", {}, el("td", { class: "muted", colspan: 4 }, "No accounts defined.")));
  for (const a of r.accounts) tb.append(el("tr", {}, el("td", {}, a.name), el("td", {}, a.kind === "api_key" ? "API key" : "Login"), el("td", { class: "wrap" }, a.key_env || a.config_dir || "(default login)"),
    el("td", {}, el("button", { class: "b d", onclick: async () => { if (await ask("Delete account " + a.name, "It is taken away from every user it is assigned to.", "Delete", true)) act(async () => { await api("POST", "/admin/api/accounts/" + a.name + "/delete", {}); loadAccounts(); }, "Account deleted"); } }, "Delete"))));
}
$("ak").onchange = () => { const k = $("ak").value === "api_key"; $("aelab").classList.toggle("hide", !k); $("adlab").classList.toggle("hide", k); };
$("asave").onclick = () => act(async () => { await api("POST", "/admin/api/accounts", { name: $("an").value, kind: $("ak").value, key_env: $("ae").value, config_dir: $("ad").value }); $("an").value = $("ae").value = $("ad").value = ""; loadAccounts(); }, "Account saved");

async function loadInstances() {
  const h = await act(() => api("GET", "/admin/api/health"));
  if (h) $("health").replaceChildren(...[["Version", h.version], ["Disk free", bytes(h.disk_free) + " of " + bytes(h.disk_total)], ["Users", h.users], ["Instances up", h.instances], ["HTTPS", h.tls ? "yes" : "no"], ["Server home", h.home]]
    .map(([k, v]) => el("div", {}, el("span", {}, k), String(v))));
  const r = await act(() => api("GET", "/admin/api/instances")); if (!r) return; const tb = $("it"); tb.replaceChildren();
  for (const i of r.instances) {
    const b = (t, a) => el("button", { class: "b", onclick: () => act(async () => { await api("POST", "/admin/api/instances/" + i.username + "/" + a, {}); loadInstances(); }, t + " done") }, t);
    tb.append(el("tr", {}, el("td", {}, i.username), el("td", {}, el("span", { class: "chip " + (i.state === "running" ? "ok" : i.state === "idle" ? "warn" : "") }, i.state)),
      el("td", { class: "num" }, i.port || ""), el("td", { class: "num" }, dur(i.uptime)), el("td", { class: "num" }, i.state === "stopped" ? "" : i.active_runs),
      el("td", { class: "act" }, i.state === "stopped" ? b("Start", "start") : [b("Stop", "stop"), b("Restart", "restart")],
        el("button", { class: "b", onclick: () => showLog(i.username) }, "Log"))));
  }
}
async function showLog(name) { const r = await act(() => api("GET", "/admin/api/instances/" + name + "/log?lines=200")); if (!r) return; const p = $("ilog"); p.classList.remove("hide"); p.textContent = "[" + name + "]\\n" + (r.log || "(empty)"); p.scrollTop = p.scrollHeight; }
$("irefresh").onclick = loadInstances;

async function loadAudit() {
  const r = await act(() => api("GET", "/admin/api/audit?n=" + $("an2").value)); if (!r) return; const tb = $("aut"); tb.replaceChildren();
  for (const e of r.events) {
    const { time, event, user, ip, by, ...rest } = e;
    tb.append(el("tr", {}, el("td", {}, (time || "").replace("T", " ").slice(0, 19)), el("td", {}, event), el("td", {}, user || ""), el("td", {}, by || ip || ""), el("td", { class: "wrap muted" }, Object.entries(rest).map(([k, v]) => k + "=" + (typeof v === "object" ? JSON.stringify(v) : v)).join(" "))));
  }
}
$("an2").onchange = $("auref").onclick = loadAudit;
show(TABS.some(([k]) => "#" + k === location.hash) ? location.hash.slice(1) : "users");
</script></body></html>"""

ACCOUNT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Account - AgentDesk</title><style>""" + STYLE + """</style></head><body>
<header><h1>Account: __USER__</h1><a href="/">Dashboard</a><a href="/admin" id="adm" class="hide">Admin</a></header>
<main><section style="max-width:420px"><h2>Change password</h2>
<form id="f" class="row" style="flex-direction:column;align-items:stretch">
<label>Current password<input id="cur" type="password" autocomplete="current-password" required></label>
<label>New password (at least 8 characters)<input id="new" type="password" autocomplete="new-password" required minlength="8"></label>
<label>New password again<input id="new2" type="password" autocomplete="new-password" required></label>
<button class="b p" type="submit">Change password</button></form>
<p class="muted">After the change you are signed out everywhere and sign in again with the new password.</p></section></main>
<div id="msg" role="status"></div>
<script>""" + COMMON_JS + """
fetch("/gateway/me").then((r) => r.json()).then((m) => { if (m.role === "admin") $("adm").classList.remove("hide"); });
$("f").onsubmit = async (ev) => {
  ev.preventDefault();
  if ($("new").value !== $("new2").value) return say("The new passwords differ", true);
  const r = await act(() => api("POST", "/account/password", { current: $("cur").value, new: $("new").value }));
  if (r) { say("Password changed; signing in again..."); setTimeout(() => location.href = "/login", 1200); }
};
</script></body></html>"""
