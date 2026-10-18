"""공개 체험판은 다른 방문자의 회사 파일을 읽거나 쓰지 않는다."""

import pytest

from lib import company as C, cost as K, public_demo, worklog as W


def test_public_demo_blocks_shared_company_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPLY_PUBLIC_DEMO", "1")
    monkeypatch.setattr(C, "COMPANY_DIR", tmp_path)
    (tmp_path / "settings.json").write_text("invalid json", encoding="utf-8")
    (tmp_path / "재고.csv").write_text("private", encoding="utf-8")
    (tmp_path / "cost_assumptions.json").write_text('[{"이름":"private"}]', encoding="utf-8")

    assert public_demo.enabled()
    assert isinstance(C.load_settings(), C.Settings)
    assert all(value is None for value in C.load_saved().values())
    assert K.load_assumptions() == []
    assert W.read_events() == []

    with pytest.raises(ValueError, match="공개 체험판"):
        C.save_raw("inventory", b"private")
    with pytest.raises(ValueError, match="공개 체험판"):
        C.save_settings(C.Settings())
    with pytest.raises(ValueError, match="공개 체험판"):
        C.clear_saved()
    with pytest.raises(ValueError, match="공개 체험판"):
        K.save_assumption({"이름": "test"})
    with pytest.raises(ValueError, match="공개 체험판"):
        W.append_event("제목", "담당", "2026-10-18", "확인 필요", "대응", "", {})
    assert (tmp_path / "재고.csv").read_text(encoding="utf-8") == "private"


def test_local_mode_remains_available(monkeypatch):
    monkeypatch.delenv("SUPPLY_PUBLIC_DEMO", raising=False)
    assert not public_demo.enabled()
