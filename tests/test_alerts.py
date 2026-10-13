"""알림: 같은 경보는 한 번만 기록하고 발송한다."""
import json

import pytest

from lib import alerts, live

FOUND = [
    {"rule": "stock", "level": "경고", "key": "stock:4304:2026-07", "message": "돼지 재고 과잉"},
    {"rule": "price", "level": "주의", "key": "gap:4304:삼겹살:2026-10", "message": "삼겹살 평년 대비 +12%"},
]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path)
    return tmp_path


def test_dedup_and_history(monkeypatch, isolated):
    toasts = []
    monkeypatch.setattr(alerts, "find_alerts", lambda cfg: [dict(a) for a in FOUND])
    monkeypatch.setattr(alerts, "_toast", lambda t, b: toasts.append((t, b)) or True)
    first = alerts.evaluate()
    second = alerts.evaluate()
    assert first["new"] == 2 and first["delivered"]
    assert second["new"] == 0
    assert len(toasts) == 1  # 두 번째 실행은 발송하지 않음
    hist = json.loads((isolated / "_alerts.json").read_text(encoding="utf-8"))
    assert len(hist) == 2 and all(h["delivered"] for h in hist)
    assert "[경고] 돼지 재고 과잉" in (isolated / "alerts.log").read_text(encoding="utf-8")


def test_new_alert_added_later_is_sent(monkeypatch):
    found = [dict(FOUND[0])]
    monkeypatch.setattr(alerts, "find_alerts", lambda cfg: [dict(a) for a in found])
    monkeypatch.setattr(alerts, "_toast", lambda t, b: True)
    alerts.evaluate()
    found.append(dict(FOUND[1]))
    assert alerts.evaluate()["new"] == 1


def test_test_mode_uses_separate_history(monkeypatch, isolated):
    monkeypatch.setattr(alerts, "find_alerts", lambda cfg: [dict(a) for a in FOUND])
    monkeypatch.setattr(alerts, "_toast", lambda t, b: True)
    alerts.evaluate(test=True)
    assert (isolated / "_alerts_test.json").exists() and not (isolated / "_alerts.json").exists()


def test_toast_failure_still_records(monkeypatch, isolated):
    monkeypatch.setattr(alerts, "find_alerts", lambda cfg: [dict(a) for a in FOUND])
    monkeypatch.setattr(alerts, "_toast", lambda t, b: False)
    res = alerts.evaluate()
    assert res["new"] == 2 and not res["delivered"]
    assert (isolated / "_alerts.json").exists()
