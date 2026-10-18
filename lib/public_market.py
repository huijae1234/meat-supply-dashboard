"""공개 체험판의 공공데이터를 안전하고 제한적으로 준비한다.

Streamlit의 여러 방문자가 같은 서버 프로세스를 사용하므로 갱신 잠금과 재시도
간격도 세션 상태가 아닌 모듈 상태로 관리한다. API 키는 Streamlit Secrets가
제공하는 서버 환경변수에서만 읽으며 반환값에는 키와 원본 오류를 넣지 않는다.
"""

from __future__ import annotations

import shutil
import threading
import time
from datetime import date, datetime, timezone

from lib import fx, live, public_demo


SNAPSHOT_DIR = live.ROOT / "data" / "public_live"
SNAPSHOT_FILES = (
    "stock.csv",
    "price_daily.csv",
    "price_month.csv",
    "auction_beef.csv",
    "auction_pig.csv",
    "fx.csv",
)
SUCCESS_TTL_SECONDS = 6 * 60 * 60
RETRY_TTL_SECONDS = 15 * 60
EKAPE_MAX_CALLS = 50
FX_MAX_CALLS = 8

_snapshot_lock = threading.Lock()
_state_lock = threading.Lock()
_worker: threading.Thread | None = None
_worker_running = False
_last_attempt_at: float | None = None
_last_key_state: tuple[bool, bool] | None = None
_last_attempt_ok = False
_last_result: dict[str, object] | None = None


def _now_monotonic() -> float:
    return time.monotonic()


def _restore_snapshots() -> bool:
    """알려진 공개 CSV만 복원하며, 이미 수집된 파일은 절대 덮어쓰지 않는다."""
    complete = True
    for filename in SNAPSHOT_FILES:
        source = SNAPSHOT_DIR / filename
        target = live.LIVE_DIR / filename
        if not source.is_file() or target.exists():
            continue
        created = False
        try:
            live.LIVE_DIR.mkdir(parents=True, exist_ok=True)
            # 독점 생성으로 다른 서버 작업이 먼저 만든 파일도 보호한다.
            with source.open("rb") as reader, target.open("xb") as writer:
                created = True
                shutil.copyfileobj(reader, writer)
        except FileExistsError:
            continue
        except OSError:
            complete = False
            # 이 호출이 만든 미완성 파일만 치우고 기존 데이터는 보존한다.
            if created:
                try:
                    target.unlink()
                except OSError:
                    pass
    return complete


def _safe_status(
    key_state: tuple[bool, bool], status: str, *, checked_at: str | None = None, collected: bool = False
) -> dict[str, object]:
    """API 응답이나 예외를 포함하지 않는 화면 표시용 상태만 만든다."""
    return {
        "enabled": True,
        "ekape_configured": key_state[0],
        "fx_configured": key_state[1],
        "checked_at": checked_at,
        "collected": collected,
        "status": status,
    }


def _refresh_in_background(key_state: tuple[bool, bool], snapshots_ok: bool) -> None:
    """Streamlit 호출 스레드 밖에서 한 번만 수집하고, 결과는 안전한 상태로 축약한다."""
    global _last_attempt_ok, _last_result, _worker_running

    collected = False
    api_ok = True
    try:
        if key_state[0]:
            try:
                result = live.refresh(
                    days_back=7,
                    stock_from=date.today().strftime("%Y-%m"),
                    max_calls=EKAPE_MAX_CALLS,
                )
                collected = any(result.get("new_rows", {}).values()) or collected
                api_ok = not result.get("errors") and api_ok
            except Exception:
                api_ok = False

        if key_state[1]:
            try:
                result = fx.collect(days_back=7, budget=FX_MAX_CALLS)
                collected = bool(result.get("new_days")) or collected
                api_ok = not result.get("error") and api_ok
            except Exception:
                api_ok = False
    except Exception:
        # 수집기 밖에서 일어난 예상 밖 오류도 화면에 원문을 전달하지 않는다.
        api_ok = False
    finally:
        complete = api_ok and snapshots_ok
        status = "ready" if complete else "partial"
        safe = _safe_status(
            key_state,
            status,
            checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            collected=collected,
        )
        with _state_lock:
            _last_attempt_ok = complete
            _last_result = safe
            _worker_running = False


def ensure_public_data() -> dict[str, object]:
    """공개 모드에서만 스냅샷과 소규모 최신 API 갱신을 준비한다.

    성공 후 6시간, 실패 후 15분 동안 중복 호출을 막는다. 방문자의 새로고침이나
    탭 이동은 API 호출권을 늘리지 않는다. 모든 반환값은 화면에 표시해도 되는
    상태 정보로만 구성한다.
    """
    global _worker, _worker_running, _last_attempt_at, _last_key_state, _last_attempt_ok, _last_result

    if not public_demo.enabled():
        return {
            "enabled": False,
            "ekape_configured": False,
            "fx_configured": False,
            "checked_at": None,
            "collected": False,
            "status": "local",
        }

    with _snapshot_lock:
        try:
            snapshots_ok = _restore_snapshots()
        except Exception:
            snapshots_ok = False
    key_state = (live.has_key(), fx.has_key())
    now = _now_monotonic()

    with _state_lock:
        if _worker_running:
            previous = _last_result or {}
            return _safe_status(
                key_state,
                "updating",
                checked_at=previous.get("checked_at"),
                collected=bool(previous.get("collected", False)),
            )

        interval = SUCCESS_TTL_SECONDS if _last_attempt_ok else RETRY_TTL_SECONDS
        if (
            _last_attempt_at is not None
            and key_state == _last_key_state
            and now - _last_attempt_at < interval
            and _last_result is not None
        ):
            return dict(_last_result)

        _last_attempt_at = now
        _last_key_state = key_state
        if not any(key_state):
            _last_attempt_ok = snapshots_ok
            _last_result = _safe_status(
                key_state,
                "unconfigured",
                checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            return dict(_last_result)

        _worker_running = True
        _worker = threading.Thread(
            target=_refresh_in_background,
            args=(key_state, snapshots_ok),
            daemon=True,
            name="public-market-refresh",
        )
        try:
            _worker.start()
        except Exception:
            _worker_running = False
            _last_attempt_ok = False
            _last_result = _safe_status(
                key_state,
                "partial",
                checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            return dict(_last_result)
        previous = _last_result or {}
        return _safe_status(
            key_state,
            "updating",
            checked_at=previous.get("checked_at"),
            collected=bool(previous.get("collected", False)),
        )
