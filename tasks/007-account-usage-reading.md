+++
id = "007"
title = "Read the real account usage and pause at the threshold"
tier = "standard"
+++

## Goal
AgentDesk must know the account's current usage percentage, show it, and pause runs at `pause_at_utilization`
(default 0.85). Right now `data/accounts/default.json` stores `"utilization": null` for five_hour, so the
pause never triggers and the dashboard gauge shows nothing, while the real usage is 37%.

Two parts:

1. **Automatic reading.** Find where the Claude usage percentage is available to a local tool (e.g. the Claude
   CLI's `/usage` output, a statusline value, or a documented field in the SDK's rate-limit events) and have
   AgentDesk read it before each run and after each attempt. Save it to `data/accounts/<name>.json` with a
   timestamp and the source. If the SDK event has `utilization: null`, fall back to the other source.
   Record in the report which source was found; if none works, say so and do part 2 only.
2. **Manual value.** In the dashboard's account row, a field "Current usage: [ ] %" with a Save button. It stores
   the value and time as `source = "manual"`. Use it when the automatic reading is missing or older than a set
   time (e.g. 30 minutes; put it in `[limits] usage_max_age_min = 30` in `data/settings.toml` as a default, without
   rewriting the user's values).

3. **Per-run cost in percent.** Record the reading just before and just after each run (and each attempt).
   Store the difference per run (`usage_before`, `usage_after`, `usage_used_pct` in the run entry in `state.json`).
   The dashboard shows, per task, "used about X% of your window", and an average per tier (e.g. "standard tasks
   average Y%"), so the user can judge how many tasks fit in the remaining percentage. If a reading is missing
   for a run, show "unknown" for that run instead of 0.

## Pause rules
- Before each run and each attempt, the pause check uses the newest reading.
- If there is no reading at all, or it's older than `usage_max_age_min`, the run does not start and the dashboard
  says "usage unknown — enter it under Accounts" (don't silently run).
- If the reading is at or above `pause_at_utilization`, the run stops with the existing message
  "usage limit N% reached the pause threshold".
- The dashboard gauge shows the value, its source, and its age.

## Where
- `agentdesk/core.py` (`Meter.see`, `account_usage`, `save_account_usage`, `stop_reason`; the reading helper).
- `agentdesk/server.py` (`account_rows`, a POST endpoint to save the manual value).
- `agentdesk/web/index.html` (account row: gauge, source, age, manual field).
- `data/settings.toml` (add `usage_max_age_min` default only).
- `README.md` (Accounts section).

## Constraints
- Don't change the run pipeline beyond the pause check.
- Don't store credentials or tokens; only the percentage, time, and source.
- Keep existing `data/accounts/*.json` readable (old files without the new fields still load).
- Test with a temporary `AGENTDESK_HOME`; keep the user's `data/` intact.

## Done when
- The dashboard shows the real usage for the `default` account, or the manual value with source and age.
- A run is refused with "usage unknown" when there is no fresh reading.
- A run stops at the threshold, with the message above (tested with a fake reading of 0.90).
- Unit tests cover: null reading, stale reading, manual value, and threshold stop.
- Each run in `state.json` has before/after readings, and the dashboard shows the per-task and per-tier percentages.
- The report says which source was used for the automatic reading, or that none was found.
