"""The admin cabinet (/admin, role admin only) and the self-service /account page.

Every route checks the role on the server (a normal user gets 403). Passwords, hashes, session ids and API keys are
never put into an answer: accounts show only their name, kind and the *name* of the environment variable that holds a
key. Mutating routes are POST (CSRF-checked by the gateway before they get here).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

from .admin_page import ACCOUNT_PAGE, ADMIN_PAGE
from .instances import APP, InstanceError
from .users import ITEM, ROLES, USERNAME, write_atomic

KINDS = ("subscription", "api_key")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
BEGIN = "# >>> agentdesk-gateway accounts (managed by the admin cabinet; edits here are overwritten)\n"
END = "# <<< agentdesk-gateway accounts\n"
SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_-]+|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
GIT_URL = re.compile(r"(https?://|ssh://|git@|git://|file://|/|[A-Za-z]:[\\/])")
PROJECT_TIMEOUT = 900


class AdminError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def human_error(e: Exception) -> str:
    return e.args[0] if e.args and isinstance(e.args[0], str) else str(e)


def positive_int(v: str, default: int, cap: int) -> int:
    return min(cap, int(v)) if v.isdigit() and int(v) > 0 else default


# ----------------------------------------------------------------------------- Claude accounts (server-wide)

class AccountStore:
    """`<server-home>/accounts.json`: {"accounts": [{name, kind, key_env | config_dir}]}. No key is stored anywhere:
    an api_key account names the environment variable (of the instance's process) that holds it."""

    def __init__(self, home: Path):
        self.path = home / "accounts.json"

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise AdminError(f"{self.path} can't be read: {e}", 503)
        rows = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        return [a for a in rows if isinstance(a, dict) and ITEM.fullmatch(str(a.get("name", "")))]

    def list(self) -> list[dict]:
        return sorted(self._load(), key=lambda a: a["name"])

    def names(self) -> set[str]:
        return {a["name"] for a in self._load()}

    def public(self) -> list[dict]:
        return [{"name": a["name"], "kind": a.get("kind", "subscription"),
                 "key_env": a.get("key_env", "") if a.get("kind") == "api_key" else "",
                 "config_dir": a.get("config_dir", "") if a.get("kind") != "api_key" else ""} for a in self.list()]

    def put(self, name: str, kind: str, key_env: str = "", config_dir: str = "") -> None:
        if not ITEM.fullmatch(name or ""):
            raise AdminError("account names are letters, digits, . _ - (up to 80)")
        if kind not in KINDS:
            raise AdminError("kind must be subscription or api_key")
        entry: dict = {"name": name, "kind": kind}
        if kind == "api_key":
            if not ENV_NAME.fullmatch(key_env or ""):
                raise AdminError("an API-key account needs the name of the environment variable that holds the key "
                                 "(not the key itself)")
            entry["key_env"] = key_env
        else:
            if any(ord(c) < 32 for c in config_dir) or len(config_dir) > 400:
                raise AdminError("bad config folder")
            entry["config_dir"] = config_dir
        rows = [a for a in self._load() if a["name"] != name] + [entry]
        write_atomic(self.path, json.dumps({"accounts": sorted(rows, key=lambda a: a["name"])}, indent=2))

    def delete(self, name: str) -> bool:
        rows = self._load()
        keep = [a for a in rows if a["name"] != name]
        if len(keep) == len(rows):
            return False
        write_atomic(self.path, json.dumps({"accounts": keep}, indent=2))
        return True


def toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def sync_accounts(user_home: Path, assigned: list[str], defs: list[dict]) -> None:
    """Put the user's assigned accounts into `<user home>/settings.toml` as a managed block (the instance reads its
    accounts from there; a list in settings.toml replaces the built-in one). No assigned account: the block goes."""
    f = user_home / "settings.toml"
    try:
        text = f.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    a, b = text.find(BEGIN), text.find(END)
    had = a != -1 and b > a
    if had:
        text = text[:a] + text[b + len(END):]
    by_name = {d["name"]: d for d in defs}
    block = ""
    for n in assigned:
        d = by_name.get(n)
        if not d:
            continue
        block += f"[[accounts]]\nname = {toml_str(n)}\nkind = {toml_str(d.get('kind', 'subscription'))}\n"
        if d.get("kind") == "api_key":
            block += f"key_env = {toml_str(d['key_env'])}\n\n"
        else:
            block += f"config_dir = {toml_str(d.get('config_dir', ''))}\n\n"
    if not block and not had:
        return
    text = text.rstrip("\n")
    out = (text + "\n\n" if text else "") + (BEGIN + block + END if block else "")
    user_home.mkdir(parents=True, exist_ok=True)
    f.write_text(out.rstrip("\n") + "\n" if out.strip() else "", encoding="utf-8")


# ----------------------------------------------------------------------------- helpers

def tail_lines(path: Path, max_lines: int, max_bytes: int = 256 * 1024) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-max_lines:]


def redact(text: str, *secrets_: str) -> str:
    for s in secrets_:
        if s:
            text = text.replace(s, "***")
    return SECRET.sub("***", text)


class Admin:
    def __init__(self, gw):
        self.gw = gw
        self.accounts = AccountStore(gw.home)

    # -- users
    def user_rows(self) -> list[dict]:
        running = {i["username"] for i in self.gw.instances.status()}
        return [{"username": u["username"], "role": u["role"], "disabled": u["disabled"], "created": u["created"],
                 "accounts": u["accounts"], "running": u["username"] in running} for u in self.gw.users.list()]

    def need_user(self, name: str) -> dict:
        u = self.gw.users.get(name) if USERNAME.fullmatch(name or "") else None
        if u is None:
            raise AdminError("no such user", 404)
        return u

    def other_admins(self, name: str) -> int:
        return sum(1 for u in self.gw.users.list() if u["role"] == "admin" and not u["disabled"] and u["username"] != name)

    def create_user(self, d: dict, by: str) -> dict:
        try:
            return self.gw.users.add(str(d.get("username", "")).strip().lower(), str(d.get("password", "")),
                                     str(d.get("role", "user") or "user"), by)
        except ValueError as e:
            raise AdminError(human_error(e))

    def user_action(self, name: str, action: str, d: dict, by: str) -> dict:
        u = self.need_user(name)
        users, mgr = self.gw.users, self.gw.instances
        last_admin = u["role"] == "admin" and not u["disabled"] and not self.other_admins(name)
        try:
            if action == "password":
                users.set_password(name, str(d.get("password", "")), by)
            elif action == "disable":
                if name == by:
                    raise AdminError("you can't disable yourself")
                if last_admin:
                    raise AdminError("that is the last enabled admin")
                users.set_disabled(name, True, by)
                mgr.stop(name, "user disabled")
            elif action == "enable":
                users.set_disabled(name, False, by)
            elif action == "role":
                role = str(d.get("role", ""))
                if role not in ROLES:
                    raise AdminError("bad role")
                if role != "admin" and u["role"] == "admin" and (name == by or last_admin):
                    raise AdminError("you can't demote yourself" if name == by else "that is the last enabled admin")
                users.set_role(name, role, by)
            elif action == "delete":
                if d.get("confirm") != name:
                    raise AdminError("type the user name to confirm")
                if name == by:
                    raise AdminError("you can't delete yourself")
                if last_admin:
                    raise AdminError("that is the last enabled admin")
                mgr.stop(name, "user removed")
                users.remove(name, by, delete_data=str(d.get("delete_data", "")) in ("1", "true", "on"))
                try:
                    (self.gw.home / "instances" / f"{name}.token").unlink()
                except OSError:
                    pass
            elif action == "accounts":
                names = sorted({x.strip() for x in str(d.get("accounts", "")).split(",") if x.strip()})
                known = self.accounts.names()
                for x in names:
                    if x not in known:
                        raise AdminError(f"no such account: {x}")
                users.assign(name, accounts=names, by=by)
                sync_accounts(Path(u["home"]), names, self.accounts.list())
            else:
                raise AdminError("unknown action", 404)
        except ValueError as e:
            raise AdminError(human_error(e))
        return {"ok": True}

    # -- accounts
    def put_account(self, d: dict, by: str) -> None:
        name = str(d.get("name", "")).strip()
        self.accounts.put(name, str(d.get("kind", "")), str(d.get("key_env", "")).strip(), str(d.get("config_dir", "")).strip())
        self.gw.audit.log("account saved", by=by, account=name)
        self.resync()

    def delete_account(self, name: str, by: str) -> None:
        if not self.accounts.delete(name):
            raise AdminError("no such account", 404)
        for u in self.gw.users.list():
            if name in u["accounts"]:
                self.gw.users.assign(u["username"], accounts=[a for a in u["accounts"] if a != name], by=by)
        self.gw.audit.log("account removed", by=by, account=name)
        self.resync()

    def resync(self) -> None:
        defs = self.accounts.list()
        for u in self.gw.users.list():
            try:
                sync_accounts(Path(u["home"]), u["accounts"], defs)
            except OSError:
                pass

    # -- projects (done by a child process with the user's AGENTDESK_HOME)
    def projtool(self, u: dict, *args: str) -> dict:
        argv = [self.gw.cfg.get("python") or sys.executable, "-m", "agentdesk.gateway.projtool", *args]
        if self.gw.cfg.get("run_as_users"):
            argv = ["sudo", "-n", "-u", f"agentdesk-{u['username']}", "--", *argv]
        env = {k: v for k, v in os.environ.items() if k != "AGENTDESK_SERVER_HOME"}
        env.update(AGENTDESK_HOME=u["home"], PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0")
        Path(u["home"]).mkdir(parents=True, exist_ok=True)
        try:
            r = subprocess.run(argv, cwd=APP, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=PROJECT_TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise AdminError(f"the project tool failed: {e}", 502)
        try:
            res = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise AdminError("the project tool failed: " + redact((r.stderr or r.stdout or "no answer").strip()[-300:]), 502)
        if not res.get("ok"):
            raise AdminError(redact(str(res.get("error", "failed"))))
        return res

    # -- instances / health
    def instance_rows(self) -> list[dict]:
        mgr, now = self.gw.instances, self.gw.clock()
        with mgr._lock:
            live = dict(mgr.instances)
        rows = []
        for u in self.gw.users.list():
            inst = live.get(u["username"])
            row = {"username": u["username"], "disabled": u["disabled"], "state": "stopped", "port": None, "pid": None,
                   "uptime": None, "active_runs": 0, "busy": False}
            if inst is not None and inst.alive() and not inst.stopping:
                info = mgr.busy_info(inst)
                busy = bool(info.get("busy"))
                runs = info.get("runs")
                row.update(state="running" if busy or inst.inflight else "idle", port=inst.port, pid=inst.proc.pid,
                           uptime=int(max(0, now - inst.started)), busy=busy,
                           active_runs=int(runs) if isinstance(runs, (int, bool)) else 0)
            rows.append(row)
        return rows

    def health(self) -> dict:
        from .. import __version__
        du = shutil.disk_usage(self.gw.home)
        return {"version": __version__, "disk_free": du.free, "disk_total": du.total, "home": str(self.gw.home),
                "users": self.gw.users.count(), "instances": len(self.gw.instances.status()),
                "tls": self.gw.ssl_ctx is not None, "python": sys.version.split()[0]}

    def audit_rows(self, n: int) -> list[dict]:
        rows = []
        for line in tail_lines(self.gw.audit.path, n):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
        return rows[::-1]


# ----------------------------------------------------------------------------- HTTP routing

def dispatch(h, user: dict, sess, path: str) -> bool:
    """Handle /admin*, /account*; False for any other path."""
    if path == "/account" or path.startswith("/account/"):
        account(h, user, sess, path)
        return True
    if path != "/admin" and not path.startswith("/admin/"):
        return False
    api = path.startswith("/admin/api/")
    if user["role"] != "admin":  # checked on every admin route, before anything else is looked at
        h.gw.audit.log("admin refused", user=user["username"], ip=h.ip, path=path[:200])
        if api:
            h.json({"error": "admin only"}, 403)
        else:
            from .app import message_page
            h.send(403, message_page("Admin only", "This page is for administrators."))
        return True
    try:
        if not api:
            if path.rstrip("/") == "/admin" and h.command in ("GET", "HEAD"):
                h.send(200, ADMIN_PAGE.replace("__CSRF__", sess.csrf).replace("__USER__", user["username"]).encode("utf-8"))
            else:
                h.send(404, b"not found", "text/plain")
            return True
        route_api(h, h.gw.admin, user, path[len("/admin/api/"):].strip("/").split("/"))
    except AdminError as e:
        h.json({"error": str(e)}, e.code)
    except KeyError as e:
        h.json({"error": human_error(e)}, 404)
    except (InstanceError, RuntimeError) as e:
        h.json({"error": str(e)}, 503)
    return True


def route_api(h, ad: Admin, user: dict, parts: list[str]) -> None:
    by, get, post = user["username"], h.command in ("GET", "HEAD"), h.command == "POST"
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)

    def body() -> dict:
        d = h.form()
        if d is None:
            raise AdminError("bad request body")
        return d

    n = len(parts)
    head = parts[0]
    if head == "users":
        if n == 1 and get:
            return h.json({"users": ad.user_rows(), "accounts": ad.accounts.public(), "me": by})
        if n == 1 and post:
            return h.json({"ok": True, "user": ad.create_user(body(), by)["username"]}, 201)
        if n == 3 and post and parts[2] != "projects":
            return h.json(ad.user_action(parts[1], parts[2], body(), by))
        if n >= 3 and parts[2] == "projects":
            u = ad.need_user(parts[1])
            if n == 3 and get:
                return h.json(ad.projtool(u, "list"))
            if n == 4 and parts[3] == "git" and post:
                d = body()
                url = str(d.get("url", "")).strip()
                if not url or url.startswith("-") or any(ord(c) < 32 for c in url) or not GIT_URL.match(url):
                    raise AdminError("give a git URL (https://..., ssh://..., git@host:path)")
                res = ad.projtool(u, "git", url, str(d.get("name", "")).strip())
                ad.gw.audit.log("project created", user=u["username"], by=by, project=res.get("slug"), source="git")
                return h.json(res, 201)
            if n == 4 and parts[3] == "zip" and post:
                return h.json(upload_zip(h, ad, u, by, q.get("name", [""])[0].strip()), 201)
            if n == 5 and parts[4] == "remove" and post:
                d = body()
                if not ITEM.fullmatch(parts[3]):
                    raise AdminError("not found", 404)
                res = ad.projtool(u, "remove", parts[3], "1" if str(d.get("delete_repo", "")) in ("1", "true", "on") else "0")
                ad.gw.audit.log("project removed", user=u["username"], by=by, project=parts[3])
                ad.gw.users.prune_projects(u["username"], by)  # assignments only ever name existing projects
                return h.json(res)
    elif head == "accounts":
        if n == 1 and get:
            return h.json({"accounts": ad.accounts.public()})
        if n == 1 and post:
            ad.put_account(body(), by)
            return h.json({"ok": True})
        if n == 3 and parts[2] == "delete" and post:
            ad.delete_account(parts[1], by)
            return h.json({"ok": True})
    elif head == "instances":
        if n == 1 and get:
            return h.json({"instances": ad.instance_rows()})
        if n == 3 and parts[2] == "log" and get:
            u = ad.need_user(parts[1])
            with ad.gw.instances._lock:
                inst = ad.gw.instances.instances.get(u["username"])
            text = "\n".join(tail_lines(ad.gw.home / "logs" / f"{u['username']}.log", positive_int(q.get("lines", [""])[0], 200, 1000)))
            return h.json({"log": redact(text, inst.token if inst else "")})
        if n == 3 and parts[2] in ("start", "stop", "restart") and post:
            u = ad.need_user(parts[1])
            mgr, name = ad.gw.instances, u["username"]
            if parts[2] != "start":
                mgr.stop(name, f"{parts[2]} by {by}")
            if parts[2] != "stop":
                if u["disabled"]:
                    raise AdminError("the user is disabled")
                mgr.ensure(name, Path(u["home"]))
            ad.gw.audit.log("instance " + parts[2], user=name, by=by)
            return h.json({"ok": True})
    elif head == "health" and n == 1 and get:
        return h.json(ad.health())
    elif head == "audit" and n == 1 and get:
        return h.json({"events": ad.audit_rows(positive_int(q.get("n", [""])[0], 200, 2000))})
    raise AdminError("not found", 404)


def upload_zip(h, ad: Admin, u: dict, by: str, name: str) -> dict:
    n_raw = h.headers.get("Content-Length") or ""
    if h.headers.get("Transfer-Encoding") or not n_raw.isdigit() or int(n_raw) == 0:
        raise AdminError("a Content-Length and a .zip body are required", 411)
    n = int(n_raw)
    if n > h.gw.max_body:
        h.close_connection = True
        raise AdminError("the file is too large", 413)
    up = h.gw.home / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="import-", suffix=".zip", dir=up)
    try:
        with os.fdopen(fd, "wb") as f:
            left = n
            while left > 0:
                chunk = h.rfile.read(min(65536, left))
                if not chunk:
                    raise AdminError("the upload broke off")
                f.write(chunk)
                left -= len(chunk)
        res = ad.projtool(u, "zip", tmp, name)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    ad.gw.audit.log("project created", user=u["username"], by=by, project=res.get("slug"), source="zip")
    return res


# ----------------------------------------------------------------------------- /account (every signed-in user)

def account(h, user: dict, sess, path: str) -> None:
    gw = h.gw
    if path == "/account" and h.command in ("GET", "HEAD"):
        return h.send(200, ACCOUNT_PAGE.replace("__CSRF__", sess.csrf).replace("__USER__", user["username"]).encode("utf-8"))
    if path == "/account/password" and h.command == "POST":
        d = h.form()
        if d is None:
            return h.json({"error": "bad request body"}, 400)
        name = user["username"]
        if gw.throttle.begin(name, h.ip) > 0:  # reserves the attempt atomically (parallel guesses can't pile up)
            return h.json({"error": "Too many failed attempts. Try again later."}, 429)
        if gw.users.authenticate(name, str(d.get("current", ""))) is None:
            gw.audit.log("password change refused", user=name, ip=h.ip)
            return h.json({"error": "The current password is wrong."}, 403)
        gw.throttle.success(name, h.ip)
        try:
            gw.users.set_password(name, str(d.get("new", "")), by=name)
        except ValueError as e:
            return h.json({"error": human_error(e)}, 400)
        gw.sessions.drop(h.cookie_value())
        return h.send(200, b'{"ok": true, "login": "/login"}', "application/json; charset=utf-8",
                      {"Set-Cookie": h.set_cookie("", 0)})
    h.send(404, b"not found", "text/plain")
