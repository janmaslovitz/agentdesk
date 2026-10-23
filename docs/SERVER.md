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
