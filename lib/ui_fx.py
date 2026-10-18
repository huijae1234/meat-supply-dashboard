"""환율 화면: USD 매매기준율 추이·변화율, 수입 원료 원료비 영향, 수집 상태·다시 받기, 알림·전가율 설정."""
from __future__ import annotations

import copy
import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from lib import company as C
from lib import cost as K
from lib import fx
from lib import theme
from lib.plain import md, won_big


def _pct(v: float) -> str:
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:+.1f}%"


def _settings_box(saved: C.Settings) -> None:
    with st.expander(f"환율 설정 (알림 기준 30일 ±{saved.fx_alert_pct:.1f}%, 전가율 {saved.fx_pass:.0f}%)"):
        draft = copy.deepcopy(saved)
        a, b = st.columns(2)
        draft.fx_alert_pct = float(a.number_input("알림 기준 (30일 변화 %)", 0.1, 50.0, float(saved.fx_alert_pct), 0.5, key="fx_alert"))
        draft.fx_pass = float(b.number_input("전가율 (%)", 0.0, 200.0, float(saved.fx_pass), 5.0, key="fx_pass",
                                             help="환율이 1% 오를 때 수입 원료 원화 단가가 몇 % 오르는지. 계약 조건·재고 회전에 따라 100%보다 작을 수 있습니다."))
        if st.button("환율 설정 저장", key="fx_save"):
            try:
                C.save_settings(draft)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success("저장했습니다.")
                st.rerun()


def _chart(df: pd.DataFrame, curs: list[str], layout) -> go.Figure:
    fig = go.Figure()
    index = len(curs) > 1
    for i, c in enumerate(curs):
        s = fx.series(c, df)
        if s.empty:
            continue
        y = s / s.iloc[0] * 100 if index else s
        if not index:  # USD 한 줄만 볼 때는 선 아래 옅은 그라데이션
            theme.soft_area(fig, s.index, y, c, theme.SERIES[0])
            fig.data[-1].hovertemplate = "%{x|%m/%d} USD: %{y:,.1f}원<extra></extra>"
            continue
        fig.add_trace(go.Scatter(x=s.index, y=y, name=c, line=dict(color=theme.SERIES[i % len(theme.SERIES)], width=2 if c == "USD" else 1.5),
                                 hovertemplate="%{x|%m/%d} " + c + (": %{y:.1f}" if index else ": %{y:,.1f}원") + "<extra></extra>"))
    fig.update_yaxes(title="첫날 = 100" if index else "원 (매매기준율)", tickformat=",.0f" if not index else ".0f")
    return layout(fig, "환율 추이" + (" (첫날 = 100으로 맞춤)" if index else " · USD"), 360)


def render_fx(k: float, layout) -> None:
    from lib import ui_company as UC
    st.subheader("환율")
    st.caption("한국수출입은행 현재환율(매매기준율)을 매일 모읍니다. 수입 원료는 **모두 USD 결제**로 보고, USD 변화를 수입 원료 단가 변화로 옮겨 원가 영향을 계산합니다.")
    if not fx.has_key():
        st.info("`.env`에 `KEY_KOREAEXIM=발급받은 인증키` 한 줄을 넣고 다시 실행하면 환율을 받을 수 있습니다.")
    df = fx.load()
    saved = UC.get_settings()
    _settings_box(saved)
    status = fx.last_status()
    a, b = st.columns([3, 1])
    a.caption(f"수집: {status.get('at', '아직 없음')} · 호출 {status.get('calls', 0)}건 · 새 날짜 {status.get('new_days', 0)}일"
              + (f" · 남은 날짜 {status['pending']}일" if status.get("pending") else "")
              + (f" · 오류: {status['error']}" if status.get("error") else "")
              + " · 매일 09:00 자동 수집에 함께 받습니다(당일 환율은 오전 11시 무렵 고시).")
    if b.button("빠진 날짜 받기", key="fx_collect", disabled=not fx.has_key(), width="stretch",
                help="최근 180일 중 아직 없는 영업일을 받습니다(하루 1,000건 제한, 한 번에 최대 300건)."):
        with st.spinner("환율을 받는 중..."):
            r = fx.collect(180)
        (st.error if r.get("error") else st.success)(f"호출 {r['calls']}건, 새 날짜 {r['new_days']}일" + (f" — {r['error']}" if r.get("error") else ""))
        df = fx.load()
    if df.empty:
        st.warning("저장된 환율이 없습니다.")
        return
    day, rate = fx.latest("USD", df)
    s = fx.series("USD", df)
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(theme.kpi_html("USD 매매기준율", f"{rate:,.1f}원", f"{md(day)} 고시"), unsafe_allow_html=True)
    ch30 = fx.change("USD", 30, df)
    alert = not math.isnan(ch30) and abs(ch30) >= saved.fx_alert_pct
    c2.markdown(theme.kpi_html("30일 변화", _pct(ch30), f"알림 기준 ±{saved.fx_alert_pct:.1f}%", tone="critical" if alert else None), unsafe_allow_html=True)
    c3.markdown(theme.kpi_html("7일 변화", _pct(fx.change("USD", 7, df))), unsafe_allow_html=True)
    c4.markdown(theme.kpi_html(f"{len(s)}영업일 범위", f"{s.min():,.0f}~{s.max():,.0f}원", f"{md(s.index[0])}부터"), unsafe_allow_html=True)

    others = sorted(set(df["cur_unit"]) - {"USD", "KRW"})
    curs = ["USD"] + st.multiselect("함께 볼 통화 (여럿이면 첫날 = 100으로 맞춰 비교)", others, default=[], key="fx_curs", placeholder="통화 선택")
    st.plotly_chart(_chart(df, curs, layout), width="stretch")
    st.caption("매매기준율은 은행 간 거래 기준 환율입니다. 실제 결제 환율(송금·신용장 환율, 수수료)과 다를 수 있습니다.")

    st.markdown("#### 수입 원료 원료비 영향")
    data, label = UC.current_company(note=False)
    if data is None or not data.ready():
        st.info("우리 공장 데이터(샘플 또는 올린 CSV)를 켜면 USD 변화가 수입 원료 원료비에 주는 영향을 보여 줍니다.")
        return
    if not K.has_prices(data):
        st.info("재고·입고 CSV에 단가_원kg(또는 단가_USDkg)이 있어야 금액을 계산합니다.")
        return
    chg = K.fx_changes(data, saved)
    if not chg:
        st.info("배합표에 수입 원료가 없거나 30일 변화율을 계산할 환율이 부족합니다.")
        return
    prod = K.product_costs(data, "avg", {n: v for n, (v, _) in chg.items()}, saved)
    ok = prod[prod["단가없는원료"] == ""]
    diff = float((ok["가정계획원료비"] - ok["계획원료비"]).sum())
    st.markdown(f"{label} 기준: USD 30일 변화 {ch30:+.2f}% × 전가율 {saved.fx_pass:.0f}% → 수입 원료 단가 {next(iter(chg.values()))[0]:+.2f}%, "
                f"계획 기간 원료비 **{'+' if diff > 0 else ''}{won_big(diff)}**")
    t = ok[ok["변화_원kg"].abs() > 0.5]
    if len(t):
        st.dataframe(pd.DataFrame({"제품": t["제품"], "kg당 원료비": t["원료비_원kg"].map(lambda v: f"{v:,.0f}원"),
                                   "환율 반영 후": t["가정원료비_원kg"].map(lambda v: f"{v:,.0f}원"),
                                   "변화": t["변화_원kg"].map(lambda v: f"{v:+,.0f}원"), "계획 기간 변화": (t["가정계획원료비"] - t["계획원료비"]).map(won_big)}),
                     hide_index=True, width="stretch")
    st.markdown("#### 환율 시나리오")
    moves = st.multiselect("USD 변화(%)", [-20.0, -15.0, -10.0, -5.0, 5.0, 10.0, 15.0, 20.0], default=[-10.0, -5.0, 5.0, 10.0],
                           format_func=lambda v: f"{v:+.0f}%", key="fx_moves", placeholder="변화 폭 선택")
    if moves:
        sc = K.fx_scenarios(data, saved, sorted(moves))
        st.dataframe(pd.DataFrame({
            "USD": sc["USD 변화"].map(lambda v: f"{v:+.0f}% ({rate * (1 + v / 100):,.0f}원)"),
            "수입 원료 단가": sc["수입 원료 단가"].map(lambda v: f"{v:+.1f}%"),
            "계획 기간 원료비": sc["계획 기간 원료비 변화"].map(lambda v: ("+" if v > 0 else "") + won_big(v)),
            "원료비 변화": sc["원료비 변화율"].map(_pct),
            "원료비율": [f"{a:.1f}% → {b:.1f}%" if a == a else "-" for a, b in zip(sc["원료비율 전"], sc["원료비율 후"])],
            "가장 크게 바뀌는 제품": sc["가장 크게 오르는 제품"],
        }), hide_index=True, width="stretch")
        st.caption(f"수입 원료 단가 변화 = USD 변화 × 전가율 {saved.fx_pass:.0f}%. 국산 원료는 그대로 둡니다. 명시된 생산계획 기간의 원료비만 계산합니다(가공비·물류비 제외).")
    st.caption("원료별로 다른 가정을 넣거나 국산 경락가와 함께 보려면 '원가 영향' 화면의 자동 채우기 기준을 쓰세요.")
