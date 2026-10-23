# AgentDesk Server: reliability

How the headless server (gateway + one instance per user) survives crashes, hangs and reboots without a human.
Install and setup are in the README ("Server (Debian, headless)").

## Checking on it

```
systemctl status agentdesk-server        # active (running), Type=notify, watchdog 1min
journalctl -u agentdesk-server -e        # gateway output (instance logs are files, see "Logs")
journalctl -u agentdesk-server -f        # follow
sudo agentdesk-server status             # listening, service state, health, users, disk
sudo agentdesk-server doctor             # also: /gateway/health answers, systemd watchdog set, disk
curl http://127.0.0.1:8080/gateway/health   # {"ok": true, "version": "...", "instances": {"up": 2, "down": 0}}
```

`/gateway/health` needs no login and shows no secrets or user names.

## systemd unit

`Type=notify`, `WatchdogSec=60`, `Restart=always`, `RestartSec=3`, `StartLimitIntervalSec=0` (never give up),
`After=network-online.target`, `TimeoutStopSec=45`. The package runs `systemctl enable` at install, so the server
comes back after a reboot (it is started once `agentdesk-server init` has made the first admin).

* The gateway (and every `agentdesk serve` when it runs under systemd itself) sends `READY=1` once it listens, then
  `WATCHDOG=1` every 30 s, **only if its self-check passes**: the request loop answers a loopback
  `/gateway/health`, `users.json` is readable, the disk has at least `min_free_gb` (1) free. A hung (not crashed)
  gateway stops pinging and systemd kills and restarts it after 60 s. No-op when not under systemd; no dependency
  (plain `NOTIFY_SOCKET` datagram). Instances started by the gateway do not inherit the notify variables.

## Reboot / crash behaviour

At gateway start: instances left over by a crashed gateway (`instances/<user>.pid`, checked by pid and start time) are
ended and old token files removed. At instance start, for every project:

* a stale `runner.pid` is removed: the process is gone, or the pid was reused (it started after the file was written,
  or its command line is not AgentDesk); leftover git / claude children of that dead run are killed (process group);
* tasks still `working` / `routing` with no live run go back to `pending` as a crash (`core.recover_stale`), and the
  auto-runner retries them per `[autorunner] retry_crashed` (more crashes: flagged "needs attention");
* corrupt `state.json` etc. fall back to their `.bak` as before; `git worktree prune`, and worktree folders with no
  `.git` (a half-created one) are removed;
* a chat that was mid-turn shows "interrupted by a restart" instead of "working".

Lock files (`users.json.lock`) are kernel `flock`s: they are released when the process dies, nothing to clean.

**Auto-resume.** When an instance starts, the gateway writes `was_active` in the user's data folder; it is removed only
when the instance is stopped on purpose (idle, admin). After a reboot or crash `autostart_instances = true` (default,
`server.toml`) starts those users' instances, so their auto-runner continues the queue (it respects the usage-limit
pause). With `false` instances start lazily at the user's next request.

## Guards

* **Hard timeout.** `[autorunner] run_timeout_min` (settings.toml / project.toml, default 720, 0 = off): a run older
  than this is killed with its children (process group); its tasks are then recovered as crashed.
* **Disk space.** `[autorunner] min_free_gb` (default 1): below it no run starts (logged loudly: `DISK FULL`).
* **Shutdown.** On SIGTERM (systemd stop / reboot) the gateway stops accepting, sends STOPPING=1, and every instance
  gets `shutdown_grace_seconds` (25) to stop: it stops accepting, asks its runs to end (SIGTERM), then kills the rest
  with their children. Tasks left `working` are recovered at the next start. Keep the grace below `TimeoutStopSec`.

## Logs

Rotated by size, `log_max_mb` (10) and `log_keep` (5) in `server.toml`: `audit.log`, `logs/<user>.log` (instance
output), and per project `agentdesk.log` and `runner.log`, plus the instance's `agentdesk.log`. Files are `x.log`,
`x.log.1` ... `x.log.5`. The gateway's own output goes to journald (`journalctl -u agentdesk-server`; size is limited
by `SystemMaxUse=` in `journald.conf`).

## Disk budget

The server's disk (512 GB by default) is shared by every user, so builds, worktrees and package caches are kept in
check by one server-wide budget. The settings are flat keys in `server.toml` (`agentdesk-server config set KEY VALUE`,
then restart).

```
sudo agentdesk-server storage            # budget, per user (by category, top consumers), caches, spool
sudo agentdesk-server storage --scan     # measure now instead of showing the last background measurement
sudo agentdesk-server clean --dry-run    # what the next automatic cleanup would delete
sudo agentdesk-server clean [--user ann] # do it (every deletion is in audit.log)
sudo agentdesk-server user quota ann 80  # GB; 0 = no limit; "default" = quota_gb
sudo agentdesk-server doctor             # WARN above the soft limit, FAIL at the hard limit, users over quota
```

The admin cabinet has a **Storage** tab: totals, a sortable per-user table (categories, top consumers), per project,
per cache, the dry-run list of the next cleanup and a "Clean now" button. The Users tab shows each user's usage bar
and a Quota button; every user sees a small usage bar in the dashboard's top bar.

### Limits

| Key | Default | Meaning |
| --- | --- | --- |
| `disk_total_gb` | 512 | the budget (the smaller of this and the real disk) |
| `disk_reserve_gb` | 40 | free space that must always remain; below it the hard limit applies |
| `soft_limit_pct` | 80 | warn (doctor, cabinet) and clean automatically |
| `hard_limit_pct` | 92 | refuse new runs, clones, imports and uploads, with the reason in the dashboard / cabinet / log; running work is never stopped |
| `quota_gb` | 50 | per user (0 = none); a user's own quota (`user quota`, cabinet) overrides it |
| `storage_scan_minutes` | 15 | the background walker's interval (it never runs in a request) |
| `auto_clean`, `auto_clean_interval_min` | true, 30 | clean at the soft limit (everyone) or for users over quota (only them) |

Usage is the **real file-system usage** (`shutil.disk_usage`), since other programs use the disk too. On a disk much
larger than `disk_total_gb` (shared with other services) AgentDesk's own measured data counts instead; the reserve is
always checked against the real free space, at the moment of the request.

A user over quota cannot start runs or clone / import / upload projects (the auto-runner waits and says why in the
project log); their data is cleaned first. The gate is written to each user's `server-policy.json`, which the instance
re-reads, so it applies to the dashboard, the auto-runner and the admin cabinet alike.

### What is counted

Everything in `<server-home>/users/<name>/`: repositories (`repos/`), project data (`projects/<slug>/`: tasks, chats,
run timelines, reports, logs), worktrees, build output, per-user caches. Links are never followed, so the shared
caches linked into a user's `cargo-home/` / `gradle-home/` are counted once, under the server's caches.

### Build output and worktrees (on by default on the server)

The server writes these [hygiene] values into each instance's policy. They beat the user's `settings.toml`; a
project's own `project.toml [hygiene]` still wins. Desktop installs keep `auto = false`.

| server.toml | [hygiene] | Default | |
| --- | --- | --- | --- |
| `auto_clean` | `auto` | true | clean without asking |
| | `artifacts` | true | build output of the other build kinds, below |
| `build_stale_days` | `stale_days` | 2 | build output untouched this long goes |
| `build_max_target_gb` | `max_target_gb` | 20 | per cargo target dir |
| `build_max_project_gb` | `max_total_gb` | 60 | per project; over it the oldest build output goes first |
| `worktree_ttl_hours` | `worktree_ttl_hours` | 24 | worktrees of done tasks idle this long are removed; the branch is kept |
| `max_worktrees` | `max_worktrees` | 6 | task worktrees per project; at the limit the next task waits and done ones go first |
| `git_gc_hours`, `offpeak_hours` | same | 24, "01-06" | `git gc --auto` (nice / ionice idle), off-peak, never during a run |
| `large_file_mb` | same | 50 | the task report warns about committed files above this size |

Build kinds (`hygiene.BUILD_KINDS`: folder names, markers, rules): cargo `target` (CACHEDIR.TAG; trimmed per profile
as before), `node_modules` (next to package.json, only with a lockfile, only in worktrees of done tasks), Python venvs
(`pyvenv.cfg`, done tasks only), `.pytest_cache` / `.mypy_cache` / `.ruff_cache` / `__pycache__`, `dist` / `build`
next to pyproject.toml / setup.py / package.json, gradle `build` / `.gradle`, maven `target` (pom.xml). Safety: a
folder is only deleted inside the project's repository or a worktree, never through a link, never if it holds a
`.git`, only when git says it is ignored and holds no tracked file (git failing = not deleted), never while a run is
active, a process works in that checkout, or its task is running. Half-created worktrees (no `.git`, unknown to git,
older than an hour) are removed too.

### Shared package caches

With `shared_caches = true` (default) every instance uses one server-wide cache per tool in `<server-home>/cache/<tool>`
instead of N copies: `npm_config_cache`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`, `GOMODCACHE`. Cargo and Gradle keep a home
per user (`cargo-home/`, `gradle-home/`: credentials and config stay private) whose `registry` / `git` and
`caches/modules-2` link to `cache/cargo` and `cache/gradle`. Only content-addressed downloads are shared, never a build
target. With `run_as_users = true` the caches stay per user (`<user home>/cache/<tool>`), since separate OS accounts
can't safely share one folder.

Each cache is pruned inside its own folder only, by unit (a crate, an extracted source dir, a cacache entry, a module
version ...) and by last use (file atime / mtime): entries unused for `cache_max_age_days` (30) go, then the least
recently used ones while the tool is over its cap; entries used within `cache_min_age_hours` (2) are never touched.
`cache_caps_gb = { cargo = 30, npm = 15, pip = 10, uv = 15, go = 10, gradle = 20 }`. `cache_dedupe = true` hard-links
identical files inside a cache (skipped while an instance is busy).

`shared_target = true` gives each user one `CARGO_TARGET_DIR` per project (`<user home>/cargo-target/<slug>/target`)
shared by that project's worktrees (unless the project sets its own in `[gates.env]`), capped at
`shared_target_max_gb` (30).

### Git, uploads, logs

* Projects cloned from a URL: `clone_depth = N` (shallow, all branch tips) and `partial_clone = true`
  (`--filter=blob:none`).
* Uploads: `max_upload_mb` (200) for project zips (also bounded by `max_body_mb`); refused before the body is read
  at the hard limit or over quota. The upload spool `<server-home>/uploads` and each user's `imports/` are emptied
  after use and swept after `temp_max_age_hours` (24).
* Logs and chats: rotated as above (`log_max_mb`, `log_keep`); run timelines and project logs are capped by
  `[hygiene] max_runs_mb` / `max_log_mb`.

Every automatic deletion (what, which user / project, bytes, why) goes to `audit.log` as `storage deleted`. That
includes deletions by an instance's own hygiene loop: it writes `<user home>/storage-audit.jsonl`, which the gateway
moves into `audit.log` at each scan.

### Sizing a 512 GB host

| | GB |
| --- | --- |
| reserve (`disk_reserve_gb`), OS, journald, packages | 40 + ~20 |
| shared caches (sum of `cache_caps_gb`) | 100 |
| users: 8 x `quota_gb` 50 | 400 (the soft limit at 410 GB used keeps this honest) |

Each user's 50 GB covers a few repositories, up to `max_worktrees` worktrees and `build_max_project_gb` of build
output per project. For heavy Rust projects, lower `build_max_target_gb` (or turn on `shared_target`) rather than
raising quotas. With more users, lower `quota_gb` so that users x quota + caches stays under about 80 % of the disk.
Over-commit is fine (not everyone is full at once): the soft limit cleans everyone, and the hard limit protects the
system.
