"""공개 체험판의 API 호출량과 데이터 보존을 네트워크 없이 확인한다."""

import json
import threading
import time
from datetime import date

import pytest

from lib import fx, live, public_demo, public_market as M


def finish_refresh():
    worker = M._worker
    assert worker is not None
    worker.join(timeout=3)
    assert not worker.is_alive()


@pytest.fixture
def public_setup(monkeypatch, tmp_path):
    snapshots = tmp_path / "public_live"
    live_dir = tmp_path / "live"
    snapshots.mkdir()
    monkeypatch.setattr(M, "SNAPSHOT_DIR", snapshots)
    monkeypatch.setattr(live, "LIVE_DIR", live_dir)
    monkeypatch.setattr(M, "_snapshot_lock", threading.Lock())
    monkeypatch.setattr(M, "_state_lock", threading.Lock())
    monkeypatch.setattr(M, "_worker", None)
    monkeypatch.setattr(M, "_worker_running", False)
    monkeypatch.setattr(M, "_last_attempt_at", None)
    monkeypatch.setattr(M, "_last_key_state", None)
    monkeypatch.setattr(M, "_last_attempt_ok", False)
    monkeypatch.setattr(M, "_last_result", None)
    monkeypatch.setattr(public_demo, "enabled", lambda: True)
    monkeypatch.setattr(live, "has_key", lambda: False)
    monkeypatch.setattr(fx, "has_key", lambda: False)
    monkeypatch.setattr(live, "refresh", lambda **_: pytest.fail("unexpected EKAPE call"))
    monkeypatch.setattr(fx, "collect", lambda **_: pytest.fail("unexpected FX call"))
    return snapshots, live_dir


def test_local_mode_has_no_effect(monkeypatch, public_setup):
    snapshots, live_dir = public_setup
    (snapshots / "stock.csv").write_text("snapshot", encoding="utf-8")
    monkeypatch.setattr(public_demo, "enabled", lambda: False)

    status = M.ensure_public_data()

    assert status["status"] == "local"
    assert not live_dir.exists()


def test_only_known_snapshots_are_restored_and_existing_data_is_kept(public_setup):
    snapshots, live_dir = public_setup
    (snapshots / "stock.csv").write_text("old snapshot", encoding="utf-8")
    (snapshots / "fx.csv").write_text("public FX", encoding="utf-8")
    (snapshots / "private.csv").write_text("must not copy", encoding="utf-8")
    live_dir.mkdir()
    (live_dir / "stock.csv").write_text("newer live data", encoding="utf-8")

    status = M.ensure_public_data()

    assert status["status"] == "unconfigured"
    assert (live_dir / "stock.csv").read_text(encoding="utf-8") == "newer live data"
    assert (live_dir / "fx.csv").read_text(encoding="utf-8") == "public FX"
    assert not (live_dir / "private.csv").exists()


def test_api_refresh_is_shared_and_bounded(monkeypatch, public_setup):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(M, "_now_monotonic", lambda: clock[0])
    monkeypatch.setattr(live, "has_key", lambda: True)
    monkeypatch.setattr(fx, "has_key", lambda: True)

    def refresh(**kwargs):
        calls.append(("ekape", kwargs))
        return {"new_rows": {"stock": 2}, "errors": []}

    def collect(**kwargs):
        calls.append(("fx", kwargs))
        return {"new_days": 1, "error": ""}

    monkeypatch.setattr(live, "refresh", refresh)
    monkeypatch.setattr(fx, "collect", collect)

    first = M.ensure_public_data()
    assert first["status"] == "updating"
    finish_refresh()
    again = M.ensure_public_data()
    clock[0] += M.SUCCESS_TTL_SECONDS - 1
    within_ttl = M.ensure_public_data()

    assert again["status"] == "ready"
    assert again["collected"] is True
    assert again["ekape_configured"] is True
    assert again["fx_configured"] is True
    assert again == within_ttl
    assert len(calls) == 2
    assert calls[0][1]["days_back"] == 7
    assert calls[0][1]["max_calls"] == M.EKAPE_MAX_CALLS == 50
    assert calls[0][1]["stock_from"] == date.today().strftime("%Y-%m")
    assert calls[1][1] == {"days_back": 7, "budget": M.FX_MAX_CALLS}

    clock[0] += 1
    assert M.ensure_public_data()["status"] == "updating"
    finish_refresh()
    assert len(calls) == 4


def test_failure_has_short_retry_and_never_exposes_raw_error(monkeypatch, public_setup):
    snapshots, live_dir = public_setup
    (snapshots / "stock.csv").write_text("public data", encoding="utf-8")
    clock = [100.0]
    calls = []
    monkeypatch.setattr(M, "_now_monotonic", lambda: clock[0])
    monkeypatch.setattr(live, "has_key", lambda: True)

    def refresh(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("SECRET-KEY-123 raw upstream response")
        return {"new_rows": {}, "errors": []}

    monkeypatch.setattr(live, "refresh", refresh)

    assert M.ensure_public_data()["status"] == "updating"
    finish_refresh()
    failed = M.ensure_public_data()
    clock[0] += M.RETRY_TTL_SECONDS - 1
    M.ensure_public_data()
    assert len(calls) == 1
    assert failed["status"] == "partial"
    assert "SECRET-KEY-123" not in json.dumps(failed)
    assert "upstream" not in json.dumps(failed)
    assert (live_dir / "stock.csv").read_text(encoding="utf-8") == "public data"

    clock[0] += 1
    assert M.ensure_public_data()["status"] == "updating"
    finish_refresh()
    retried = M.ensure_public_data()
    assert retried["status"] == "ready"
    assert len(calls) == 2
    assert (live_dir / "stock.csv").read_text(encoding="utf-8") == "public data"


def test_new_key_bypasses_previous_unconfigured_cooldown(monkeypatch, public_setup):
    clock = [100.0]
    configured = [False]
    calls = []
    monkeypatch.setattr(M, "_now_monotonic", lambda: clock[0])
    monkeypatch.setattr(live, "has_key", lambda: configured[0])
    monkeypatch.setattr(live, "refresh", lambda **kwargs: calls.append(kwargs) or {"new_rows": {}, "errors": []})

    assert M.ensure_public_data()["status"] == "unconfigured"
    configured[0] = True
    clock[0] += 1
    assert M.ensure_public_data()["status"] == "updating"
    finish_refresh()
    assert M.ensure_public_data()["status"] == "ready"
    assert len(calls) == 1


def test_fx_error_is_reduced_to_safe_status(monkeypatch, public_setup):
    monkeypatch.setattr(fx, "has_key", lambda: True)
    monkeypatch.setattr(
        fx,
        "collect",
        lambda **_: {"new_days": 0, "error": "SECRET-KEY-123 private API response"},
    )

    assert M.ensure_public_data()["status"] == "updating"
    finish_refresh()
    status = M.ensure_public_data()

    assert status["status"] == "partial"
    assert status["fx_configured"] is True
    assert "SECRET-KEY-123" not in json.dumps(status)


def test_first_render_is_nonblocking_and_concurrent_visitor_reuses_worker(monkeypatch, public_setup):
    snapshots, live_dir = public_setup
    (snapshots / "stock.csv").write_text("public data", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    calls = []
    monkeypatch.setattr(live, "has_key", lambda: True)

    def slow_refresh(**kwargs):
        calls.append(kwargs)
        started.set()
        assert release.wait(timeout=5)
        return {"new_rows": {}, "errors": []}

    monkeypatch.setattr(live, "refresh", slow_refresh)
    try:
        began = time.perf_counter()
        first = M.ensure_public_data()
        elapsed = time.perf_counter() - began
        assert elapsed < 1.0
        assert first["status"] == "updating"
        assert (live_dir / "stock.csv").read_text(encoding="utf-8") == "public data"
        assert started.wait(timeout=1)

        second = M.ensure_public_data()
        assert second["status"] == "updating"
        assert len(calls) == 1
    finally:
        release.set()
        finish_refresh()

    assert M.ensure_public_data()["status"] == "ready"
