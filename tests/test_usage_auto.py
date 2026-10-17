import json
import time
import urllib.error

import pytest

from agentdesk import core, server


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(core, "usage_auto_enabled", lambda: True)
    (core.DATA / "settings.toml").unlink(missing_ok=True)
    (core.DATA / "accounts").mkdir(parents=True, exist_ok=True)
    for f in (core.DATA / "accounts").glob("*.json"):
        f.unlink()
    yield


def _login(tmp_path, monkeypatch, expires_in=3600):
    cfg = tmp_path / "claude"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "SECRET-TOKEN", "expiresAt": (time.time() + expires_in) * 1000}}), encoding="utf-8")
    monkeypatch.setattr(core, "credentials_file", lambda name: cfg / ".credentials.json")


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


API = {"five_hour": {"utilization": 70.0, "resets_at": "2026-10-16T16:10:00+00:00"},
       "seven_day": {"utilization": 15.0, "resets_at": "2026-10-23T02:00:00+00:00"}, "seven_day_opus": None}


def test_refresh_saves_the_higher_window_as_the_reading_without_the_token(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch)
    seen = {}

    def fake(req, timeout=0):
        seen["auth"] = req.get_header("Authorization")
        return _Resp(API)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    assert core.refresh_usage("default", force=True) is None
    assert seen["auth"] == "Bearer SECRET-TOKEN"
    r = core.current_reading("default")
    assert r["source"] == "api" and r["utilization"] == 0.70 and r["five_hour"] == 0.70 and r["seven_day"] == 0.15
    assert "SECRET-TOKEN" not in (core.DATA / "accounts" / "default.json").read_text(encoding="utf-8")


def test_refresh_is_throttled(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: calls.append(1) or _Resp(API))
    core.refresh_usage("default", force=True)
    core.refresh_usage("default")
    core.refresh_usage("default")
    assert len(calls) == 1


def test_failure_keeps_the_manual_reading_and_reports_why(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch)
    core.set_manual_usage("default", 40)

    def boom(req, timeout=0):
        raise urllib.error.HTTPError("u", 401, "no", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    err = core.refresh_usage("default", force=True)
    assert "sign-in rejected" in err
    r = core.current_reading("default")
    assert r["source"] == "manual" and r["utilization"] == 0.40
    assert server.auto_error("default") == err
    assert "SECRET-TOKEN" not in err


def test_expired_token_and_missing_login_give_clear_reasons(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch, expires_in=-10)
    assert "expired" in core.refresh_usage("default", force=True)
    monkeypatch.setattr(core, "credentials_file", lambda name: tmp_path / "nowhere" / ".credentials.json")
    assert "not signed in" in core.refresh_usage("default", force=True)


def test_auto_reading_can_be_switched_off(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch)
    monkeypatch.setattr(core, "usage_auto_enabled", lambda: False)  # after the fixture turned it on
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("must not call the service"))
    assert core.refresh_usage("default", force=True) is None
    assert server.auto_error("default") is None


def _http_error(code, retry_after=None):
    import email.message
    h = email.message.Message()
    if retry_after:
        h["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("u", code, "x", h, None)


def test_a_busy_service_makes_us_back_off_and_wait(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch)
    calls = []

    def busy(req, timeout=0):
        calls.append(1)
        raise _http_error(429)

    monkeypatch.setattr("urllib.request.urlopen", busy)
    err = core.refresh_usage("default", force=True)
    assert "rate-limiting" in err and "next try at" in err and len(calls) == 1
    first = core.account_usage("default")["auto"]
    assert first["fails"] == 1 and first["next"]
    assert core.refresh_usage("default", min_interval_s=0) == err and len(calls) == 1  # waits: no new request
    core.refresh_usage("default", force=True)  # forced (e.g. the user pressed something): asks again, waits longer
    second = core.account_usage("default")["auto"]
    assert second["fails"] == 2 and second["next"] > first["next"]


def test_the_services_retry_after_is_obeyed_and_success_resets_the_backoff(tmp_path, monkeypatch):
    import datetime as dt
    _login(tmp_path, monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: (_ for _ in ()).throw(_http_error(429, 600)))
    core.refresh_usage("default", force=True)
    nxt = dt.datetime.fromisoformat(core.account_usage("default")["auto"]["next"])
    assert 500 < (nxt - dt.datetime.now()).total_seconds() <= 600
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: _Resp(API))
    assert core.refresh_usage("default", force=True) is None
    auto = core.account_usage("default")["auto"]
    assert auto["fails"] == 0 and auto["next"] is None and auto["error"] is None
    assert core.current_reading("default")["source"] == "api"


def test_other_failures_do_not_back_off(tmp_path, monkeypatch):
    _login(tmp_path, monkeypatch)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: (_ for _ in ()).throw(OSError("offline")))
    assert "couldn't reach" in core.refresh_usage("default", force=True)
    assert core.account_usage("default")["auto"]["next"] is None  # next normal poll, no long wait
