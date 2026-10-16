"""새 화면 두 개: '오늘의 현황'(실제 데이터 중심)과 '우리 공장'(CSV 업로드 + 계산).

app.py가 이미 커서 화면 코드를 여기로 뺐다. 차트 스타일(layout)과 색은 app.py에서 받아 쓴다.
"""
from __future__ import annotations

import hmac
import os
import hashlib

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from lib import advice as A
from lib import company as C
from lib import live
from lib import theme
from lib.plain import VERDICT, VERDICT_ICON, days, md, ton, vs_normal, vs_year, change_text



def live_sig() -> str:
    d = live.LIVE_DIR
    return "|".join(f"{p.name}:{p.stat().st_mtime_ns}" for p in sorted(d.glob("*.csv"))) if d.exists() else ""


@st.cache_data(ttl=300)
def market_cached(sig: str, k: float) -> A.Market:
    return A.market_snapshot(k)


# ------------------------------------------------------------------ 접근 제한 (우리 공장 데이터만)
def password_set() -> bool:
    return bool(os.environ.get("COMPANY_PASSWORD"))


def unlocked() -> bool:
    """비밀번호가 정해져 있지 않으면 항상 열려 있다. 정해져 있으면 이 브라우저 세션에서 맞게 입력해야 열린다."""
    return (not password_set()) or bool(st.session_state.get("co_unlocked"))


def render_lock(where: str = "") -> bool:
    """잠겨 있으면 비밀번호 입력칸을 보이고 True(= 아직 잠김)를 돌려준다."""
    if unlocked():
        return False
    st.info("🔒 우리 공장 데이터는 비밀번호가 필요합니다." + (f" ({where})" if where else ""))
    pw = st.text_input("비밀번호", type="password", key=f"co_pw_{where}")
    if pw:
        if hmac.compare_digest(pw.encode("utf-8"), os.environ.get("COMPANY_PASSWORD", "").encode("utf-8")):
            st.session_state["co_unlocked"] = True
            st.rerun()
        else:
            st.error("비밀번호가 맞지 않습니다.")
    return True


# ------------------------------------------------------------------ 자사 데이터 상태 공유
def get_settings() -> C.Settings:
    """화면과 자동 알림은 동일한 저장 설정만 사용한다."""
    try:
        return C.load_settings()
    except (ValueError, KeyError, OSError) as exc:
        st.error(f"설정을 읽을 수 없어 계산을 중단했습니다: {exc}")
        st.stop()


def current_company() -> tuple[C.CompanyData | None, str]:
    """(자사 데이터, 출처 이름). 올린 파일이 없고 샘플도 안 켰으면 (None, '')."""
    if st.session_state.get("co_source") == "sample":
        return C.load_data(C.sample_tables()), "샘플(가짜) 데이터"
    saved = C.load_saved()
    if not any(v is not None for v in saved.values()):
        return None, ""
    return C.load_data(saved), "올려 둔 파일"


# ------------------------------------------------------------------ 오늘의 현황
def _company_block(m: A.Market) -> bool:
    """'우리 공장' 구역을 그린다. 실제로 계산 결과를 보여줬으면 True."""
    st.markdown("#### 우리 공장: 오늘 할 일")
    if render_lock("오늘의 현황"):
        return False
    data, label = current_company()
    if data is None or not data.ready():
        if data is not None:
            st.error("계산 불완전: 공장 입력 오류를 수정하기 전에는 생산·발주 판단을 표시하지 않습니다.")
            for issue in data.issues:
                if issue.level == "오류":
                    st.write(issue.message)
        st.info("**우리 공장** 화면에 재고·입고 예정·생산계획·배합표 CSV를 올리면, 여기에 원료별 소진 예정일과 "
                "**발주 검토안**(날짜·톤)이 나옵니다.")
        return False
    proj = C.project(data, today=C.today_kst(), settings=get_settings())
    if not proj.actionable:
        st.warning(C.freshness(data, C.today_kst()))
        return True
    from lib.ui_planning import show_impact
    show_impact(data, compact=True)
    acts = A.company_actions(proj, m)
    counts = pd.Series([a["level"] for a in acts]).value_counts()
    st.caption(f"{label} · 재고 기준일 {md(proj.base_date)}")
    c1, c2, c3, c4 = st.columns(4)
    for col, (label, key, tone) in zip((c1, c2, c3, c4), (("긴급", "긴급", "critical"), ("발주 필요", "발주", "serious"),
                                                          ("넉넉함", "과잉", "warning"), ("이상 없음", "정상", "good"))):
        col.markdown(theme.kpi_html(label, str(int(counts.get(key, 0))), tone=tone), unsafe_allow_html=True)
    todo = [a for a in acts if a["level"] in ("긴급", "발주")]
    if todo:
        for a in todo[:6]:
            _action_card(a)
        if len(todo) > 6:
            st.caption(f"나머지 {len(todo) - 6}건은 '우리 공장' 화면에서 확인하세요.")
    else:
        st.success("지금 당장 필요한 발주나 긴급 조치는 없습니다.")
    return True


def _market_block(m: A.Market, own_first: bool) -> None:
    st.markdown("#### 시장 신호" + (" (참고)" if own_first else ""))
    cols = st.columns(2)
    for col, v in zip(cols, A.market_verdicts(m)):
        with col, st.container(border=True):
            st.markdown(f'<div class="vhead"><span class="sp">{v["species"]}</span>{theme.dot(v["signal"])}{v["verdict"]}</div>', unsafe_allow_html=True)
            st.write(v["text"])
            if v["bad_parts"]:
                st.write("특히 적은 부위: **" + ", ".join(v["bad_parts"]) + "**")
            if v["over_parts"]:
                st.write("넉넉한 부위: " + ", ".join(v["over_parts"]))


def _price_block(m: A.Market) -> None:
    st.markdown("#### 시세 (쓰는 부위)")
    if m.prices is None or not len(m.prices):
        return
    used = {C.PARTS[sp][p]["price"] for sp in C.PARTS for p in C.PARTS[sp] if C.PARTS[sp][p]["price"]}
    p = m.prices[m.prices["item"].map(lambda x: any(x.startswith(u) for u in used))].copy()
    p["최근 가격"] = p["latest_krw_kg"].map(lambda v: f"{v:,.0f}원/kg")
    p["평년 대비"] = p["gap_pct"].map(vs_year)
    p["한 달 변화"] = p["chg30_pct"].map(change_text)
    st.dataframe(p.rename(columns={"종류": "구분", "item": "부위", "streak": "추세"})[["구분", "부위", "최근 가격", "평년 대비", "한 달 변화", "추세"]],
                 hide_index=True, use_container_width=True)
    st.caption("소비자가 기준이라 우리 매입 단가와 다릅니다. 방향(오르는 중인지)을 보는 참고용입니다.")


def render_today(k: float) -> None:
    try:
        m = market_cached(live_sig(), k)
    except Exception:
        m = A.Market()
        st.warning("시장 참고 데이터를 읽지 못했습니다. 공장 계산은 계속 사용할 수 있습니다.")
    st.subheader("오늘의 생산 위험")
    st.caption("원료 기준의 계획 검토입니다. 설비·인력·포장재·완제품 재고·출하 승인·운송 시간은 포함하지 않습니다.")
    if unlocked() and st.button("샘플로 생산 차질 확인", key="today_sample"):
        st.session_state["co_source"] = "sample"
        st.rerun()
    _company_block(m)
    if not m.available:
        st.info("시장 참고 데이터가 없습니다. 공장 CSV·샘플 계산은 독립적으로 사용할 수 있습니다.")
        return
    st.caption(f"시장 재고는 **{m.stock_month:%Y-%m}** 집계가 마지막입니다(월 단위, 2~3개월 늦게 나옵니다). "
               f"소비자가 기준일은 **{md(m.price_date)}** 입니다. 시장 숫자는 전국 기준이며 우리 공장 수치가 아닙니다.")
    with st.expander("시장 참고 신호·시세"):
        _market_block(m, own_first=True)
        _price_block(m)


def _action_card(a: dict) -> None:
    with st.container(border=True):
        st.markdown(f"{theme.dot(a['level'])}**{a['headline']}**", unsafe_allow_html=True)
        for s in a["steps"]:
            st.markdown(f"- {s}")
        for s in a["market"]:
            st.caption("시장 참고: " + s)


# ------------------------------------------------------------------ 우리 공장
def _template_csv(kind: str) -> bytes:
    return C.sample_tables()[kind].to_csv(index=False).encode("utf-8-sig")  # 엑셀에서 한글이 안 깨지게 BOM 포함


def render_company(k: float, layout) -> None:
    st.subheader("우리 공장")
    if render_lock("우리 공장"):
        return
    st.caption("원료 재고·입고 예정·생산계획·배합표 CSV를 올리면 **원료별 소진 예정일과 발주 시점·수량**을 계산합니다. "
               "파일은 이 PC의 `data/company/` 에만 저장되고 git에는 올라가지 않습니다.")
    if password_set():
        st.caption("🔒 비밀번호가 설정돼 있습니다(`.env`의 `COMPANY_PASSWORD`). 이 화면과 '오늘의 현황'의 우리 공장 부분은 입력 후에만 보입니다.")
    else:
        st.warning("회사 데이터를 넣으면 같은 와이파이에서 대시보드를 여는 사람 누구나 볼 수 있습니다. "
                   "`.env`에 `COMPANY_PASSWORD=원하는비밀번호` 한 줄을 넣고 다시 실행하면 이 화면이 잠깁니다.", icon="🔒")

    top = st.columns([2, 2, 2])
    # 상태를 바꾼 뒤에는 화면을 다시 그린다 ('오늘의 현황'이 이 화면보다 먼저 그려지기 때문)
    if top[0].button("샘플(가짜) 데이터로 시험", use_container_width=True, help="날짜는 오늘 기준으로 만든 설명용 가짜 숫자입니다. 파일을 저장하지 않습니다."):
        st.session_state["co_source"] = "sample"
        st.rerun()
    if top[1].button("올려 둔 파일 사용", use_container_width=True):
        st.session_state["co_source"] = "saved"
        st.rerun()
    confirm_clear = st.checkbox("저장한 원본 CSV 4종 삭제에 동의합니다 (설정·조치 이력은 유지)")
    if top[2].button("저장된 파일 지우기", use_container_width=True, disabled=not confirm_clear):
        n = C.clear_saved()
        st.session_state["co_source"] = "saved"
        for kind in C.FILES:
            st.session_state.pop(f"saved_{kind}", None)
        st.toast(f"{n}개 파일을 지웠습니다")
        st.rerun()

    st.markdown("#### 1. 파일 올리기")
    st.caption("엑셀에서 'CSV(쉼표로 분리)' 또는 'CSV UTF-8'로 저장하면 됩니다. 양식은 아래 버튼으로 받으세요(예시 숫자는 가짜입니다).")
    cols = st.columns(2)
    for i, kind in enumerate(C.FILES):
        with cols[i % 2], st.container(border=True):
            st.markdown(f"**{C.FILE_LABELS[kind]}** `{C.FILES[kind]}`" + (" (선택)" if kind == "inbound" else ""))
            st.caption("열: " + ", ".join(C.REQUIRED[kind]) + (" · 선택: " + ", ".join(C.OPTIONAL[kind]) if C.OPTIONAL[kind] else ""))
            f = st.file_uploader(C.FILE_LABELS[kind], type="csv", key=f"up_{kind}", label_visibility="collapsed")
            if f is not None:
                fid = hashlib.sha256(f.getvalue()).hexdigest()
                if st.session_state.get(f"saved_{kind}") != fid:
                    iss: list[C.Issue] = []
                    try:
                        raw_df = C.read_csv_bytes(f.getvalue())
                        ok = C.clean_table(kind, raw_df, iss) is not None
                    except Exception as e:  # 인코딩·구조 문제
                        ok, iss = False, [C.Issue("오류", C.FILE_LABELS[kind], str(e))]
                    if ok:
                        C.save_raw(kind, f.getvalue())
                        st.session_state[f"saved_{kind}"] = fid
                        st.session_state["co_source"] = "saved"
                        st.rerun()
                    else:
                        for it in iss:
                            st.error(it.message)
            p = C.COMPANY_DIR / C.FILES[kind]
            if p.exists():
                st.caption(f"✅ 저장됨: {pd.Timestamp(p.stat().st_mtime, unit='s'):%m/%d %H:%M}")
            st.download_button("양식 내려받기", _template_csv(kind), file_name=C.FILES[kind], mime="text/csv", key=f"dl_{kind}")
    with st.expander("부위 이름 목록 (이 이름으로 적어야 시장 데이터와 연결됩니다)"):
        for sp, parts in C.PARTS.items():
            st.write(f"**{sp}**: " + ", ".join(parts))
        st.caption("원산지는 국산/수입, 보관은 냉장/냉동으로 적습니다. '앞다리'는 시장 데이터의 '전지', '뒷다리'는 '후지', '목살'은 '목심'과 연결됩니다.")

    with st.expander("기본값 조정 (CSV에 안전재고일수·리드타임일수·발주주기일을 안 적은 원료에 적용)"):
        saved_settings = get_settings()
        draft_settings = C.Settings()
        a, b, c = st.columns(3)
        a.markdown("**안전재고 (일치)**")
        for (o, sto), v in C.DEFAULT_SAFETY.items():
            draft_settings.safety[(o, sto)] = a.number_input(f"{o} {sto}", 0, 90, int(saved_settings.safety[(o, sto)]), key=f"set_safety_{o}_{sto}")
        b.markdown("**리드타임 (발주→입고 일)**")
        for o, v in C.DEFAULT_LEAD.items():
            draft_settings.lead[o] = b.number_input(o, 0, 120, int(saved_settings.lead[o]), key=f"set_lead_{o}")
        c.markdown("**발주 주기 (일)**")
        for o, v in C.DEFAULT_CYCLE.items():
            draft_settings.cycle[o] = c.number_input(o, 1, 60, int(saved_settings.cycle[o]), key=f"set_cycle_{o}")
        if st.button("기본값 저장 · 화면과 자동 알림에 함께 적용"):
            C.save_settings(draft_settings)
            st.success("저장했습니다. 화면 계산과 다음 자동 알림이 같은 설정을 사용합니다.")
        st.caption("저장 전 편집 값은 계산에 적용하지 않습니다. CSV에 적힌 원료별 값이 저장 기본값보다 우선합니다.")
        st.caption("발주량 = (안전재고 + 발주 주기 동안 쓸 양) − 입고 시점의 예상 재고. 100kg 단위로 올립니다.")

    data, label = current_company()
    st.markdown("#### 2. 결과")
    if data is None:
        st.info("파일을 올리거나 위의 '샘플(가짜) 데이터로 시험'을 눌러 보세요.")
        return
    if data.issues:
        with st.expander(f"데이터 점검 {len(data.issues)}건", expanded=any(i.level == '오류' for i in data.issues)):
            st.dataframe(pd.DataFrame([vars(i) for i in data.issues]).rename(columns={"level": "구분", "file": "파일", "message": "내용"}),
                         hide_index=True, use_container_width=True)
    if not data.ready():
        st.error("필수 파일(재고, 생산계획, 배합표)이 모두 올바르게 올라와야 계산합니다. 위 점검 내용을 확인하세요.")
        return

    try:
        m = market_cached(live_sig(), k)
    except Exception:
        m = A.Market()
    proj = C.project(data, today=C.today_kst(), settings=get_settings())
    s = proj.summary
    st.caption(f"{label} · 재고 기준일 {md(proj.base_date)} · 앞으로 {C.HORIZON_DAYS}일 계산")
    if not proj.actionable:
        st.warning(C.freshness(data, C.today_kst()) + " 아래는 과거 기준 참고 계산입니다.")

    table = pd.DataFrame({
        "상태": s["상태"], "원료": s["원료"], "보관": s["보관"],
        "보유재고": s["현재고_kg"].map(ton), "첫날 사용가능 재고": s["초기사용가능_kg"].map(ton), "하루 사용": s["일평균사용_kg"].map(ton),
        "재고일수(입고 전)": s["재고일수"].map(days), "안전재고 아래": s["안전재고이하일"].map(md),
        "소진 예정": s["소진예정일"].map(md), "발주 기한": s["발주기한"].map(md),
        "발주량(참고)": s["발주량_kg"].map(lambda v: ton(v) if v > 0 and proj.actionable else "권고 없음"), "원료 사용 비중(차질률 아님)": s["영향제품"],
    })
    st.dataframe(table, hide_index=True, use_container_width=True)

    st.markdown("#### 3. 검토할 대응 · 자동 발주 아님")
    for a in A.company_actions(proj, m):
        _action_card(a)

    st.markdown("#### 4. 예상 재고 그래프")
    pick = st.selectbox("원료", s["원료"].tolist(), key="co_pick")
    g = proj.daily[proj.daily["원료"] == pick]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=g["날짜"], y=g["예상재고_kg"] / 1000, name="예상 재고", line=dict(color=theme.BLUE, width=2)))
    fig.add_trace(go.Scatter(x=g["날짜"], y=g["안전재고_kg"] / 1000, name="안전재고", line=dict(color=theme.ORANGE, dash="dash", width=2)))
    ib = g[g["입고_kg"] > 0]
    if len(ib):
        fig.add_trace(go.Scatter(x=ib["날짜"], y=ib["예상재고_kg"] / 1000, mode="markers", name="입고 예정",
                                 marker=dict(color=theme.INK2, size=10, symbol="triangle-up")))
    fig.add_hline(y=0, line_color=theme.STATUS["critical"], line_width=1)
    fig.update_yaxes(title="톤")
    st.plotly_chart(layout(fig, f"{pick} 예상 재고", 380), use_container_width=True)
    st.caption("파란 선이 주황 점선(안전재고) 아래로 내려가면 부족 위험, 0 아래(빨간 선)로 내려가면 소진입니다. 신규 발주는 반영하지 않은 예상입니다.")

    with st.expander("계산 가정"):
        for n in proj.notes:
            st.write("· " + n)
        st.write("· 재고는 기준일 마감 값이며, 소요량은 기준일 다음 날부터 셉니다.")
        st.write("· 소요량 = 생산계획 물량 × 배합표 투입량. 계획이 없는 날은 0, 계획이 끝난 뒤는 계획 기간의 하루 평균으로 이어 갑니다.")
        st.write("· 입고 예정은 입고예정일 당일에 재고에 더합니다. 기준일 이전 입고는 이미 재고에 들어간 것으로 봅니다.")
        st.write("· 시장 신호(재고·소비자가)는 전국 기준 참고 문구이며 계산에는 영향을 주지 않습니다.")
        st.write("· 냉장·냉동은 분리합니다. 검사대기·사용보류는 제외하고, 사용가능일(해동 준비 포함) 이후·사용기한 이내 로트를 사용합니다. 기한 미입력은 제한 없이 계산합니다.")
        st.write("· 이 그래프는 계획 수요를 모두 차감한 순수급 참고선입니다. 실제 가능한 생산과 잔량은 '생산 차질·대응안'에서 확인하세요.")
