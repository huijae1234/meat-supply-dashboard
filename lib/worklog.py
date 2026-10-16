"""로컬 조치 이력. SQLite 트랜잭션으로 이전 기록을 덮어쓰지 않고 누적한다."""
import json
import sqlite3
import pandas as pd
from uuid import uuid4

from lib import company as C

STATES = ["확인 필요", "담당자 지정", "공급처 협의", "입고 일정 확정", "재계산", "해결 확인"]


def append_event(title, owner, deadline, state, action, result, context, case_id=None):
    if not title.strip() or not owner.strip() or not action.strip():
        raise ValueError("제목·담당자·대응 내용을 입력하세요.")
    if state not in STATES or (state == "해결 확인" and not result.strip()):
        raise ValueError("올바른 상태와 해결 확인 근거를 입력하세요.")
    case_id = case_id or uuid4().hex
    C.COMPANY_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(C.COMPANY_DIR / "worklog.sqlite3") as con:
        con.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, at TEXT, payload TEXT)")
        payload = {"제목": title.strip(), "담당자": owner.strip(), "기한": str(deadline), "상태": state,
                   "대응": action.strip(), "결과": result.strip(), "근거": context}
        con.execute("INSERT INTO events(case_id, at, payload) VALUES (?,?,?)", (case_id, str(pd.Timestamp.now(tz="Asia/Seoul")), json.dumps(payload, ensure_ascii=False)))
    return case_id


def read_events():
    path = C.COMPANY_DIR / "worklog.sqlite3"
    if not path.exists():
        return []
    with sqlite3.connect(path) as con:
        return [{"번호": seq, "사건ID": case_id, "기록시각": at, **json.loads(payload)}
                for seq, case_id, at, payload in con.execute("SELECT seq,case_id,at,payload FROM events ORDER BY seq DESC")]
