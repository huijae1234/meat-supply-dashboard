"""예약 실행용 수집 스크립트.  python collect.py [days_back]

- 이미 받은 날짜는 건너뛰고, 하루 호출 한도(900건)에 걸리면 최신 날짜부터 받고 멈춘다.
  못 받은 과거 날짜는 다음 실행에서 이어서 받는다. (기본 300일 이력을 이틀에 걸쳐 채움)
- 결과 요약은 data/live/collect.log 에, 개별 실패는 data/live/_errors.log 에 남는다.
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from lib import alerts, fx, live  # noqa: E402


def main(days_back: int = 300) -> int:
    live.LIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        res = live.refresh(days_back=days_back)
        issues = live.quality_check()
        fxr = fx.collect(days_back=30) if fx.has_key() else {"calls": 0, "new_days": 0, "error": "키 없음"}  # 빠진 영업일만 받는다
        al = alerts.evaluate()  # 새로 생긴 경보만 Windows 알림·로그·이력으로 남긴다
        line = (f"{stamp} 성공 호출={res['calls']} 신규={sum(res['new_rows'].values())}행 실패={len(res['errors'])} "
                f"대기={res['pending']} 품질경고={sum(1 for i in issues if i['level'] == '경고')} 신규경보={al['new']} "
                f"환율={fxr['new_days']}일" + (f"(오류: {fxr['error']})" if fxr.get("error") else ""))
        code = 0 if not res["errors"] else 1
    except Exception as e:  # 키 누락, 네트워크 단절 등 수집 전체 실패
        line, code = f"{stamp} 실패 {type(e).__name__}: {e}", 2
    with open(live.LIVE_DIR / "collect.log", "a", encoding="utf-8") as f:
        f.write(line + chr(10))
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 300))
