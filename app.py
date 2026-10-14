"""축산물(돼지·소) 수급 대시보드.  실행: streamlit run app.py"""
import warnings

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from lib import analysis as A
from lib import alerts, live
from lib import live_analysis as LA
from lib import theme
from lib import ui_company as UC
from lib.plain import VERDICT, deviation_word, model_name, vs_normal
from lib.data import OUTLIER_RATIO, SPECIES_KO, get_source, last_actual_week, load_all

warnings.filterwarnings("ignore")
st.set_page_config(page_title="고기가 부족하게 돼지 않겠소?", page_icon="🥩", layout="wide")

st.markdown(theme.CSS, unsafe_allow_html=True)

ACTUAL, FILEFC, MODEL = theme.BLUE, theme.ORANGE, theme.INK2   # 실적=파랑, 파일 예측=주황, 자체 모델=짙은 회색
RED, BLUE, GRAY = theme.STATUS["critical"], theme.BLUE, theme.MUTED  # 부호 구분: 양수=빨강, 음수=파랑 (초록·빨강 조합은 색약에서 구분이 어렵다)
METRICS = {
    "ending_stock_kg": "기말 재고 (kg)",
    "domestic_output_kg": "국내 생산량 (kg)",
    "import_volume_kg": "수입량 (kg)",
    "slaughter_heads": "도축 두수 (두)",
    "import_share_pct": "수입 비중 (%)",
}
MODELED = {"ending_stock_kg", "domestic_output_kg", "import_volume_kg"}


# ------------------------------------------------------------------ 로딩
@st.cache_resource(show_spinner="데이터를 불러오는 중...")
def get_tables(signature: str):
    return load_all(get_source())


src = get_source()
T = get_tables(src.signature())
sup, cal, imp = T["fact_supply_weekly"], T["dim_calendar"], T["fact_import_monthly"]
flow, meat = T["fact_distribution_flow_monthly"], T["fact_meat_weekly"]
cut_dim, ch_dim, reg_dim = T["dim_cut"], T["dim_channel"], T["dim_region"]
LAST = last_actual_week(sup)


def layout(fig, title=None, height=380, yfmt=None):
    """차트 공통 모양: 중립 격자, 낮은 대비의 눈금, 왼쪽 정렬 제목, 검증된 계열 색 순서."""
    for tr in fig.data:  # 점이 20개 미만이면 plotly가 마커를 붙이므로 선만 쓰도록 고정
        if tr.type == "scatter" and tr.mode is None:
            tr.mode = "lines"
    fig.update_layout(font=dict(family="Pretendard, 'Noto Sans KR', 'Malgun Gothic', sans-serif", size=13, color=theme.INK2),
                      colorway=theme.SERIES,
                      title=dict(text=title, x=0, xanchor="left", y=0.97, yanchor="top", font=dict(size=14, color=theme.INK)) if title else None,
                      height=height, margin=dict(l=10, r=10, t=80 if title else 40, b=10),
                      legend=dict(orientation="h", y=1.0, yanchor="bottom", x=0, font=dict(size=12, color=theme.INK2)), hovermode="x unified",
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                      hoverlabel=dict(bgcolor=theme.SURFACE, font_size=13, font_color=theme.INK, bordercolor=theme.GRID))
    axis = dict(tickfont=dict(size=12, color=theme.MUTED), title_font=dict(size=12, color=theme.INK2))
    fig.update_xaxes(showgrid=False, linecolor=theme.BASELINE, **axis)
    fig.update_yaxes(gridcolor=theme.GRID, gridwidth=1, zeroline=False, tickformat=yfmt, **axis)
    return fig


def shade_forecast(fig):
    fig.add_vrect(x0=LAST.to_pydatetime(), x1=sup["week_start"].max().to_pydatetime(),
                  fillcolor="rgba(11,11,11,0.04)", line_width=0)
    return fig


def fmt_kg(v):
    return f"{v / 1e3:,.0f}톤"


# ------------------------------------------------------------------ 상단 바
def _live_sig() -> str:
    return "|".join(f"{p.name}:{p.stat().st_mtime_ns}" for p in sorted(live.LIVE_DIR.glob("*.csv"))) if live.LIVE_DIR.exists() else ""


_ls = live.last_status()
_bad = bool(_ls and not _ls.get("ok", True))
_collect = f"실제 수집 실패 {_ls.get('errors', 0)}건" if _bad else f"실제 데이터 수집 {live.last_refresh() or '없음'}"
st.markdown(
    f'''<div class="hero"><div><h1>🥩🐷🐂 고기가 부족하게 돼지 않겠소?</h1>
<p>축산물 수급 대시보드 · 돼지·소 재고·가격 경보 · 수입 위험 · 유통 병목</p></div>
<div><span class="meta">합성 데이터 실적 ~{LAST:%Y-%m-%d}</span><span class="meta">{theme.dot("critical" if _bad else "good")}{_collect}</span></div></div>''',
    unsafe_allow_html=True)

# 모든 설정(육종 포함)은 우측 상단 '설정' 팝오버 안에 모은다
_, _bar_set = st.columns([8, 1.2])
with _bar_set:
    with st.popover("설정", use_container_width=True):
        species = st.radio("육종", ["pork", "beef"], format_func=SPECIES_KO.get, horizontal=True, key="species")
        st.divider()
        st.markdown("**재고 경보 기준**")
        k = st.slider("경보 민감도 (낮을수록 예민)", 0.5, 3.0, 1.5, 0.1,
                      help="평소 재고 변동폭의 몇 배 넘게 벗어나면 경보로 볼지 정합니다. 숫자가 작을수록 조금만 달라도 경보가 울립니다.")
        baseline = st.radio("기준선 (합성 데이터 화면에 적용)", ["all", "rolling52"],
                            format_func=lambda x: {"all": "전체 실적 평균", "rolling52": "직전 52주 평균"}[x])
        st.divider()
        st.caption(f"데이터 소스: {src.name} · 마지막 실적 주 {LAST:%Y-%m-%d}")
        if st.button("데이터 다시 읽기"):
            st.cache_resource.clear()
            st.rerun()

sp_ko = SPECIES_KO[species]


def page_today():
    UC.render_today(k)


def page_company():
    UC.render_company(k, layout)


def _syn_title(name: str) -> None:
    st.subheader(name)
    st.caption("합성 데이터 · 분석 방법을 보여 주는 시연용 화면입니다.")


# ------------------------------------------------------------------ 인사이트 생성
def build_insights(sp: str) -> list[tuple[str, str]]:
    out, ko = [], SPECIES_KO[sp]
    a = A.stock_alert_table(sup, sp, k, baseline)
    act = a[a["data_type"] == "actual"]
    last = act.iloc[-1]
    n = A.signal_streak(act["signal"])
    if last["signal"] != "정상":
        out.append(("bad", f"**{ko}** 재고가 {n}주 연속 **{last['signal']}** 구간입니다 (z={last['z']:+.2f}, 재고 {last['weeks_of_cover']:.1f}주치)."))
    else:
        out.append(("ok", f"**{ko}** 재고는 정상 범위입니다 (z={last['z']:+.2f}, {last['weeks_of_cover']:.1f}주치)."))
    recent = act["ending_stock_kg"].tail(5)
    if (recent.diff().dropna() < 0).all():
        out.append(("warn", f"**{ko}** 재고가 최근 4주 연속 감소 중입니다 ({fmt_kg(recent.iloc[0])} → {fmt_kg(recent.iloc[-1])})."))
    fc = a[a["data_type"] == "forecast"]
    breach = fc[fc["signal"] != "정상"]
    if len(breach):
        b = breach.iloc[0]
        out.append(("bad", f"파일 예측 기준 **{b['week_start']:%m/%d} 주**에 재고가 **{b['signal']}** 구간에 진입합니다."))
    else:
        out.append(("ok", "파일 예측 기간(13주) 중 재고 경보 구간 진입은 없습니다."))
    mf = A.forecast_frame(sup, cal, sp, "ending_stock_kg")
    if mf["gap_pct"].abs().mean() > 5:
        out.append(("warn", f"자체 모델과 파일 예측의 재고 차이가 평균 {mf['gap_pct'].abs().mean():.1f}%로 큽니다. 예측 가정 확인이 필요합니다."))
    s = sup[(sup["species"] == sp) & (sup["data_type"] == "actual")]
    share_now, share_52 = s["import_share_pct"].tail(4).mean(), s["import_share_pct"].tail(52).mean()
    d = share_now - share_52
    if abs(d) >= 1:
        out.append(("warn", f"최근 4주 수입 비중 {share_now:.1f}%는 52주 평균 대비 **{d:+.1f}%p** 입니다."))
    r = A.import_risk(imp, sp)
    r = r[r["data_type"] == "actual"].iloc[-1]
    lvl = "고집중" if r["hhi"] >= 2500 else "중간" if r["hhi"] >= 1500 else "분산"
    out.append(("warn" if lvl == "고집중" else "ok", f"수입국 집중도(HHI) {r['hhi']:,.0f} ({lvl}), 최대 {r['top_country']} {r['top_share']:.0f}%."))
    nxt = cal[(cal["week_start"] > LAST) & cal["holiday_name"].notna()]
    if len(nxt):
        out.append(("warn", f"다음 명절({nxt.iloc[0]['holiday_name']})은 {nxt.iloc[0]['week_start']:%m/%d} 주입니다. 사전 재고 확보를 점검하세요."))
    fb = A.flow_bottleneck(flow, sp)
    lm = fb[fb["month"] == fb["month"].max()]
    hot = lm[lm["score"] >= 2]
    for _, h in hot.iterrows():
        out.append(("warn", f"유통 병목 신호: {h['from_stage']} → {h['to_stage']} (리드타임 z={h['lead_z']:+.1f}, 마진 z={h['margin_z']:+.1f})."))
    return out



# ================================================================== ① 현황·경보
def page_syn_status():
    _syn_title("현황·경보")
    cols = st.columns(2)
    for col, sp in zip(cols, ["pork", "beef"]):
        a = A.stock_alert_table(sup, sp, k, baseline)
        act = a[a["data_type"] == "actual"]
        last, prev = act.iloc[-1], act.iloc[-2]
        with col:
            st.subheader(SPECIES_KO[sp])
            c1, c2 = st.columns(2)
            c1.metric("기말 재고", fmt_kg(last["ending_stock_kg"]), f"{(last['ending_stock_kg'] / prev['ending_stock_kg'] - 1) * 100:+.1f}% 전주")
            c2.markdown(theme.kpi_html("재고 신호", VERDICT[last["signal"]], f"z {last['z']:+.2f}", tone=last["signal"]), unsafe_allow_html=True)
            c3, c4 = st.columns(2)
            c3.metric("재고 회전", f"{last['weeks_of_cover']:.1f}주치", f"{last['weeks_of_cover'] - prev['weeks_of_cover']:+.2f}주", delta_color="off")
            c4.metric("수입 비중", f"{last['import_share_pct']:.1f}%", f"{last['import_share_pct'] - prev['import_share_pct']:+.1f}%p", delta_color="off")
    st.subheader(f"재고 경보 · {sp_ko}")
    a = A.stock_alert_table(sup, species, k, baseline)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=a["week_start"], y=a["upper"], line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=a["week_start"], y=a["lower"], fill="tonexty", fillcolor="rgba(11,11,11,0.05)",
                             line=dict(width=0), name=f"정상 범위 (±{k}σ)", hoverinfo="skip"))
    ac, fc = a[a["data_type"] == "actual"], a[a["data_type"] == "forecast"]
    fig.add_trace(go.Scatter(x=ac["week_start"], y=ac["ending_stock_kg"], name="실적", line=dict(color=ACTUAL, width=2)))
    fig.add_trace(go.Scatter(x=fc["week_start"], y=fc["ending_stock_kg"], name="파일 예측", line=dict(color=FILEFC, dash="dash", width=2)))
    bad = a[a["signal"] != "정상"]
    fig.add_trace(go.Scatter(x=bad["week_start"], y=bad["ending_stock_kg"], mode="markers", name="경보 주",
                             marker=dict(color=RED, size=8, line=dict(color="white", width=1))))
    shade_forecast(fig)
    st.plotly_chart(layout(fig, height=360), use_container_width=True)
    st.subheader("자동 인사이트")
    for sp in [species, "beef" if species == "pork" else "pork"]:
        st.markdown(f"**{SPECIES_KO[sp]}**")
        for lvl, txt in build_insights(sp):
            st.markdown(f"{theme.dot(lvl)}{txt}", unsafe_allow_html=True)
    st.info("합성 데이터 화면입니다. **실제 데이터 대응:** '오늘의 현황'과 '실제 분석·예측' 화면.")
    with st.expander("합성 데이터 품질"):
        err = T["meat_errors"]
        st.write(f"소비량 입력 오류 **{len(err)}건**을 그룹 중앙값으로 보정했습니다 (중앙값의 {OUTLIER_RATIO:g}배 초과).")
        st.dataframe(err.head(50), use_container_width=True, height=200)
        st.caption("fact_import_monthly 는 오류 셀이 있는 엑셀 대신 로컬 CSV를 사용합니다.")

# ================================================================== ② 수급 추이·예측
def page_syn_trend():
    _syn_title("수급 추이·예측")
    metric = st.selectbox("지표", list(METRICS), format_func=METRICS.get)
    d = sup[sup["species"] == species].sort_values("week_start")
    ac, fc = d[d["data_type"] == "actual"], d[d["data_type"] == "forecast"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ac["week_start"], y=ac[metric], name="실적", line=dict(color=ACTUAL, width=2)))
    bridge = pd.concat([ac.tail(1), fc])
    fig.add_trace(go.Scatter(x=bridge["week_start"], y=bridge[metric], name="파일 예측", line=dict(color=FILEFC, dash="dash", width=2)))
    if metric in MODELED:
        mf = A.forecast_frame(sup, cal, species, metric)
        fig.add_trace(go.Scatter(x=mf["week_start"], y=mf["hi"], line=dict(width=0), showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=mf["week_start"], y=mf["lo"], fill="tonexty", fillcolor="rgba(82,81,78,0.10)",
                                 line=dict(width=0), name="자체 모델 95% 구간", hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=mf["week_start"], y=mf["model"], name="자체 모델", line=dict(color=MODEL, dash="dot", width=2)))
    shade_forecast(fig)
    st.plotly_chart(layout(fig, f"{sp_ko} · {METRICS[metric]}", 420), use_container_width=True)

    if metric in MODELED:
        l, r = st.columns(2)
        with l:
            st.markdown("**백테스트** (마지막 13주를 숨기고 예측, MAPE 낮을수록 좋음)")
            rows = []
            for c in MODELED:
                b = A.backtest(sup, cal, species, c)
                rows.append({"지표": METRICS[c], "자체 모델": f"{b['mape_model']:.1f}%", "전년 동주 단순예측": f"{b['mape_naive']:.1f}%"})
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            st.caption("모델: 추세 + 연간 계절성 + 명절/삼겹살데이/휴가철/가정의 달 변수를 쓴 릿지 회귀.")
        with r:
            st.markdown("**파일 예측 vs 자체 모델** (선택 지표)")
            t = mf.assign(week_start=mf["week_start"].dt.strftime("%m/%d"))
            t = t.rename(columns={"week_start": "주", "file_forecast": "파일 예측", "model": "자체 모델", "gap_pct": "차이(%)"})
            st.dataframe(t[["주", "파일 예측", "자체 모델", "차이(%)"]].round(1), hide_index=True, use_container_width=True, height=280)
        g = mf["gap_pct"].abs().mean()
        st.info(f"13주 평균 절대 차이 **{g:.1f}%**. 파일 예측이 자체 모델의 95% 구간을 벗어난 주는 **{((mf['file_forecast'] > mf['hi']) | (mf['file_forecast'] < mf['lo'])).sum()}주**입니다.")
    else:
        st.caption("이 지표는 파일 예측만 표시합니다. 자체 모델은 재고·국내 생산·수입량에 제공됩니다.")
    st.info("합성 데이터 화면입니다. **실제 데이터 대응:** '실제 분석·예측' 화면의 월 재고·일 소비자가 예측(백테스트 포함).")

# ================================================================== ③ 계절성·이상치
def page_syn_season():
    _syn_title("계절성·이상치")
    metric3 = st.selectbox("지표 ", ["slaughter_heads", "domestic_output_kg", "import_volume_kg", "ending_stock_kg"], format_func=METRICS.get)
    s = A.stl_decompose(sup, species, metric3)
    thr = st.slider("이상치 기준 |z| (STL 잔차)", 2.0, 5.0, 3.0, 0.5)
    l, r = st.columns(2)
    with l:
        d = sup[(sup["species"] == species) & (sup["data_type"] == "actual")].copy()
        d["year"], d["wk"] = d["week_start"].dt.isocalendar().year.astype(int), d["week_start"].dt.isocalendar().week.astype(int)
        fig = go.Figure()
        for y, g in d.groupby("year"):
            fig.add_trace(go.Scatter(x=g["wk"], y=g[metric3], name=str(y), mode="lines"))
        fig.update_xaxes(title="ISO 주차")
        st.plotly_chart(layout(fig, "연도별 겹쳐보기", 340), use_container_width=True)
    with r:
        fig = go.Figure(go.Scatter(x=s["week_start"], y=s["seasonal"], line=dict(color=MODEL), name="계절 성분"))
        st.plotly_chart(layout(fig, "계절 성분 (STL)", 340), use_container_width=True)
    an = s[s["resid_z"].abs() >= thr]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=s["week_start"], y=s["value"], name="실적", line=dict(color=ACTUAL)))
    fig.add_trace(go.Scatter(x=s["week_start"], y=s["trend"] + s["seasonal"], name="추세+계절", line=dict(color=GRAY, dash="dot")))
    fig.add_trace(go.Scatter(x=an["week_start"], y=an["value"], mode="markers", name="이상 주", marker=dict(color=RED, size=9)))
    st.plotly_chart(layout(fig, f"이상 주 {len(an)}개", 360), use_container_width=True)
    if len(an):
        t = an.assign(주=an["week_start"].dt.strftime("%Y-%m-%d"), 실적=an["value"].round(0), 기대=(an["trend"] + an["seasonal"]).round(0), z=an["resid_z"].round(1))
        t["편차(%)"] = ((t["실적"] / t["기대"] - 1) * 100).round(1)
        t = t.merge(cal[["week_start", "holiday_name"]].assign(주=lambda x: x["week_start"].dt.strftime("%Y-%m-%d")).drop(columns="week_start"), on="주", how="left")
        t = t.rename(columns={"holiday_name": "명절"})
        st.dataframe(t[["주", "실적", "기대", "편차(%)", "z", "명절"]].sort_values("z", key=abs, ascending=False), hide_index=True, use_container_width=True)
    st.info("합성 데이터 화면입니다. 실제 도축·생산 주간 데이터를 제공하는 API를 찾지 못했습니다 (docs/API_SURVEY.md).")

# ================================================================== ④ 명절·이벤트
def page_syn_event():
    _syn_title("명절·이벤트")
    cuts = cut_dim[cut_dim["species"] == species]
    pick = st.selectbox("부위", ["전체"] + cuts["cut_id"].tolist(), format_func=lambda x: "전체" if x == "전체" else cuts.set_index("cut_id").loc[x, "cut_name_ko"])
    cons = A.weekly_consumption(meat, species, None if pick == "전체" else pick)
    ev = A.event_index(cons, cal)
    l, r = st.columns(2)
    with l:
        hc = A.holiday_curve(ev)
        fig = go.Figure(go.Bar(x=hc["weeks_to_holiday"], y=hc["mean"] - 100, marker_color=[RED if v >= 0 else BLUE for v in hc["mean"] - 100],
                               text=[f"{v:+.0f}%" for v in hc["mean"] - 100], textposition="outside"))
        fig.update_xaxes(title="명절 주 기준 (0 = 명절 주, 음수 = 이전)", dtick=1)
        fig.update_yaxes(title="평소 대비 %")
        st.plotly_chart(layout(fig, "명절 전후 소비 변화 (설·추석 평균)", 360), use_container_width=True)
    with r:
        rows = []
        for name, flag in [("삼겹살데이 주", "is_samgyeopsal_day_week"), ("여름 휴가철", "is_summer_vacation_season"), ("가정의 달", "is_family_month")]:
            on, off = ev.loc[ev[flag] == 1, "index"], ev.loc[ev[flag] == 0, "index"]
            rows.append({"이벤트": name, "해당 주 지수": round(on.mean(), 1) if len(on) else np.nan, "그 외 지수": round(off.mean(), 1), "차이(%p)": round(on.mean() - off.mean(), 1) if len(on) else np.nan, "주 수": len(on)})
        st.markdown("**이벤트 효과** (지수 100 = 평소)")
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption("지수 = 주간 소비량 ÷ 13주 중심 이동 중앙값 × 100. 표본(주 수)이 적은 이벤트는 참고용입니다.")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=ev["week_start"], y=ev["index"], line=dict(color=ACTUAL), name="소비 지수"))
    hol = ev[ev["holiday_name"].notna()]
    fig.add_trace(go.Scatter(x=hol["week_start"], y=hol["index"], mode="markers+text", text=hol["holiday_name"].map({"seollal": "설", "chuseok": "추석"}),
                             textposition="top center", marker=dict(color=FILEFC, size=10), name="명절 주"))
    fig.add_hline(y=100, line_dash="dot", line_color=GRAY)
    st.plotly_chart(layout(fig, "주간 소비 지수", 340), use_container_width=True)
    st.info("재고 관점: 명절 **직전 주**까지 재고를 확보하고, 명절 **다음 주**(소비 피크 후행)의 보충 물량을 점검하세요.")
    st.info("합성 데이터 화면입니다. 실제 소비량 데이터를 제공하는 API를 찾지 못했습니다.")

# ================================================================== ⑤ 수입국·단가
def page_syn_import():
    _syn_title("수입국·단가")
    r = A.import_risk(imp, species)
    d = imp[imp["species"] == species]
    cc = theme.country_colors(d["country"].unique())  # 같은 나라는 모든 차트에서 같은 색
    l, rr = st.columns(2)
    with l:
        fig = go.Figure()
        for c, g in d.groupby("country"):
            fig.add_trace(go.Scatter(x=g["month"], y=g["import_share_pct"], name=c, stackgroup="one", mode="lines",
                                     line=dict(color=cc[c], width=1), fillcolor=theme.rgba(cc[c], 0.55)))
        fig.update_yaxes(title="%")
        shade = fig.add_vrect(x0=LAST.to_pydatetime(), x1=d["month"].max().to_pydatetime(), fillcolor="rgba(11,11,11,0.04)", line_width=0)
        st.plotly_chart(layout(fig, "수입국 비중 추이", 360), use_container_width=True)
    with rr:
        fig = go.Figure()
        for c, g in d.groupby("country"):
            fig.add_trace(go.Scatter(x=g["month"], y=g["cif_price_usd_kg"], name=c, mode="lines", line=dict(color=cc[c], width=2)))
        fig.add_vrect(x0=LAST.to_pydatetime(), x1=d["month"].max().to_pydatetime(), fillcolor="rgba(11,11,11,0.04)", line_width=0)
        fig.update_yaxes(title="USD/kg")
        st.plotly_chart(layout(fig, "수입 단가 (CIF)", 360), use_container_width=True)
    l, rr = st.columns(2)
    with l:
        # 집중도(HHI) = 나라별 비중(%)의 제곱 합. 나라별 기여분을 선으로 그리고 합계를 굵은 선으로 겹친다.
        cur = r[r["data_type"] == "actual"].iloc[-1]
        last_d = d[d["month"] == cur["month"]].sort_values("import_share_pct", ascending=False)
        top_y = max(3000, r["hhi"].max() * 1.15)
        fig = go.Figure()
        for y0, y1, color, name in [(0, 1500, "rgba(11,11,11,0.00)", "분산"), (1500, 2500, "rgba(11,11,11,0.04)", "중간"),
                                    (2500, top_y, "rgba(208,59,59,0.07)", "고집중")]:
            fig.add_hrect(y0=y0, y1=min(y1, top_y), fillcolor=color, line_width=0, annotation_text=name,
                          annotation_position="top right", annotation_font_size=12)
        for i, c in enumerate(last_d["country"]):
            g = d[d["country"] == c].sort_values("month")
            style = dict(color=cc[c], dash="dash", width=2) if c == "기타" else dict(color=cc[c], width=2)
            fig.add_trace(go.Scatter(x=g["month"], y=g["import_share_pct"] ** 2, name=f"{c} 기여", mode="lines", line=style))
        fig.add_trace(go.Scatter(x=r["month"], y=r["hhi"], line=dict(color=theme.INK, width=3), name="합계 = 집중도(HHI)"))
        fig.add_trace(go.Scatter(x=[cur["month"]], y=[cur["hhi"]], mode="markers+text", text=[f"{cur['hhi']:,.0f}"],
                                 textposition="top left", marker=dict(color=RED, size=10), showlegend=False))
        fig.update_yaxes(title="집중도 점수 (나라별 비중²)", range=[0, top_y])
        fig = layout(fig, "수입국 집중도 추이", 460)
        fig.update_layout(legend=dict(orientation="h", x=0, y=-0.18, yanchor="top"), margin=dict(l=10, r=10, t=60, b=10))
        st.plotly_chart(fig, use_container_width=True)
        parts = ", ".join(f"{row['country']} {row['import_share_pct']:.0f}%→{row['import_share_pct'] ** 2:,.0f}" for _, row in last_d.head(3).iterrows())
        st.caption(f"**읽는 법:** 얇은 선은 나라별 기여(그 나라 비중의 제곱), 굵은 선은 그 합계인 집중도(HHI)입니다. "
                   f"비중을 제곱하므로 큰 나라일수록 기여가 급격히 커집니다. 가장 최근 달: {parts}. "
                   "점선 '기타'는 여러 나라를 묶은 값이라 실제 기여를 과소 계산합니다. 배경 구간(분산 1,500 미만 · 중간 · 고집중 2,500 이상)은 통상 기준이며 이 대시보드에 맞게 검증한 값은 아닙니다.")
    with rr:
        fig = go.Figure(go.Bar(x=r["month"], y=r["cif_yoy_pct"], marker_color=[RED if v > 0 else BLUE for v in r["cif_yoy_pct"].fillna(0)]))
        st.plotly_chart(layout(fig, "평균 CIF 단가 전년 동월 대비 (%)", 300), use_container_width=True)
    st.caption("월 합계는 4/5주 차이가 있어 주 평균(weekly_avg_kg)으로 환산해 비교합니다. '기타'는 여러 국가를 묶은 값이라 집중도가 낮게 계산될 수 있습니다.")
    st.info("합성 데이터 화면입니다. 관세청 수출입실적 API 후보가 있으나 신청·검증 전입니다 (docs/API_SURVEY.md). 실제 수입 소비자가는 '실제 데이터' 화면 참고.")

# ================================================================== ⑥ 가격·소비
def page_syn_price():
    _syn_title("가격·소비")
    cuts = cut_dim[cut_dim["species"] == species]
    pick6 = st.selectbox("부위 ", ["전체"] + cuts["cut_id"].tolist(), format_func=lambda x: "전체" if x == "전체" else cuts.set_index("cut_id").loc[x, "cut_name_ko"], key="p6")
    ps = A.price_series(meat, species, None if pick6 == "전체" else pick6)
    l, r = st.columns(2)
    with l:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=ps["week_start"], y=ps["wholesale"], name="도매", line=dict(color=ACTUAL)))
        fig.add_trace(go.Scatter(x=ps["week_start"], y=ps["retail"], name="소매", line=dict(color=FILEFC)))
        fig.update_yaxes(title="원/kg")
        st.plotly_chart(layout(fig, "도매 vs 소매 가격", 340), use_container_width=True)
    with r:
        lc = A.lag_correlation(ps)
        fig = go.Figure(go.Bar(x=lc["lag"], y=lc["corr"], marker_color=[ACTUAL if v < lc["corr"].max() else FILEFC for v in lc["corr"]],
                               text=lc["corr"].round(2), textposition="outside"))
        fig.update_xaxes(title="소매가 반영 지연 (주)", dtick=1)
        st.plotly_chart(layout(fig, "도매가 변화 → 소매가 변화 상관", 340), use_container_width=True)
        best = int(lc.loc[lc["corr"].idxmax(), "lag"])
        st.caption(f"상관이 가장 큰 지연: **{best}주**. 데이터 설명상 소매가는 도매가 대비 1~2주 지연 반영됩니다.")
    l, r = st.columns(2)
    with l:
        fig = go.Figure(go.Scatter(x=ps["week_start"], y=ps["spread"], line=dict(color=MODEL)))
        st.plotly_chart(layout(fig, "소매-도매 스프레드 (원/kg)", 300), use_container_width=True)
    with r:
        dim = st.radio("소비 분해", ["채널", "지역", "원산지"], horizontal=True)
        m2 = meat[(meat["species"] == species) & (meat["data_type"] == "actual")]
        if pick6 != "전체":
            m2 = m2[m2["cut_id"] == pick6]
        key = {"채널": "channel_id", "지역": "region_id", "원산지": "origin"}[dim]
        lab = {"channel_id": ch_dim.set_index("channel_id")["channel_name_ko"], "region_id": reg_dim.set_index("region_id")["region_short"]}
        g = m2.groupby(key)["consumption_kg"].sum().sort_values()
        g.index = [lab[key].get(i, i) if key in lab else {"domestic": "국산", "import": "수입"}[i] for i in g.index]
        fig = go.Figure(go.Bar(x=g.values / 1e6, y=g.index, orientation="h", marker_color=ACTUAL))
        fig.update_xaxes(title="누적 소비량 (천 톤)")
        st.plotly_chart(layout(fig, f"{dim}별 누적 소비", 300), use_container_width=True)
    st.caption(f"소매가 1% 상승 시 소비 {price_e:+.2f}%" if (price_e := A.price_elasticity(ps)) < 0 else
               f"주의: 소비-가격 단순 로그 상관이 양(+{price_e:.2f})입니다. 수요가 가격을 끌어올리는 효과가 섞여 있어 인과적 탄력성으로 해석하지 마세요.")
    st.info("합성 데이터 화면입니다. **실제 데이터 대응:** '실제 데이터' 화면(소비자가·경락가·국산 vs 수입)과 '실제 분석·예측' 화면(경락가→소비자가 시차). 실제 소비량 데이터는 없습니다.")

# ================================================================== ⑦ 유통 병목
def page_syn_flow():
    _syn_title("유통 병목")
    fb = A.flow_bottleneck(flow, species)
    fb["flow"] = fb["from_stage"] + " → " + fb["to_stage"]
    l, r = st.columns(2)
    with l:
        piv = fb.pivot_table(index="flow", columns="month", values="lead_z")
        fig = go.Figure(go.Heatmap(z=piv.values, x=[m.strftime("%y-%m") for m in piv.columns], y=piv.index, colorscale="RdYlGn_r", zmid=0, colorbar=dict(title="z")))
        st.plotly_chart(layout(fig, "리드타임 z-score (구간별 자체 이력 기준)", 340), use_container_width=True)
    with r:
        piv = fb.pivot_table(index="flow", columns="month", values="margin_z")
        fig = go.Figure(go.Heatmap(z=piv.values, x=[m.strftime("%y-%m") for m in piv.columns], y=piv.index, colorscale="RdYlGn_r", zmid=0, colorbar=dict(title="z")))
        st.plotly_chart(layout(fig, "마진 z-score", 340), use_container_width=True)
    nb = A.node_balance(flow, species)
    nb["rel"] = nb.groupby("node")["ratio"].transform(lambda s: s / s.mean() * 100)
    fig = go.Figure()
    for n, g in nb.groupby("node"):
        fig.add_trace(go.Scatter(x=g["month"], y=g["rel"], name=n))
    fig.add_hline(y=100, line_dash="dot", line_color=GRAY)
    st.plotly_chart(layout(fig, "노드 유출/유입 비율 (자기 평균=100, 낮을수록 물량이 쌓이는 중)", 300), use_container_width=True)
    top = fb.sort_values("score", ascending=False).head(8)
    st.markdown("**병목 신호 상위 구간·월**")
    st.dataframe(top.assign(월=top["month"].dt.strftime("%Y-%m"), 리드타임=top["lead"].round(2), 마진=top["margin"].round(0))[["월", "flow", "리드타임", "lead_z", "마진", "margin_z", "score"]].round(2),
                 hide_index=True, use_container_width=True)
    st.caption("한 구간의 평소 리드타임·마진 대비 얼마나 벗어났는지(z)로 병목을 봅니다. 유출/유입 비율은 구조적으로 1이 아니어서 평균 대비 변화만 해석하세요.")
    st.info("합성 데이터 화면입니다. 단계별 물량·마진·리드타임을 제공하는 공개 데이터를 찾지 못했습니다. 경락가→소비자가 스프레드가 마진의 근사치입니다.")

def _bt(df: pd.DataFrame) -> pd.DataFrame:
    """백테스트 표를 현장 말로 (모델 이름, 오차 열 이름)."""
    return df.assign(모델=df["모델"].map(model_name)).rename(columns={"MAPE(%)": "평균 오차(%)"}).round(2)


# ================================================================== ⑧ 실제 데이터 (API)
KIND = {"pork": 4304, "beef": 4301}
KIND_IMP = {"pork": 4402, "beef": 4401}


@st.cache_data(ttl=300)
def live_data(sig: str):
    return {n: live.load(n) for n in live.SCHEMAS}




# ================================================================== ⑨ 실제 분석·예측
def page_analysis():
    st.subheader("실제 데이터 분석·예측")
    st.caption("저장된 실제 API 데이터(`data/live/`)만 사용합니다. 표본이 작아 **보조 지표**로 보세요: 월 재고 31개, 일별 가격 수개월, 경락가 수개월.")
    L = live_data(_live_sig())
    stk9, pdl9, pm9, ab9, ap9 = L["stock"], L["price_daily"], L["price_month"], L["auction_beef"], L["auction_pig"]
    if stk9.empty or pdl9.empty:
        st.info("실제 데이터가 아직 없습니다. 먼저 수집을 실행하세요: `python collect.py`")
    else:
        kd9, sp9 = KIND[species], sp_ko
        # ---------------- 재고 경보
        st.markdown("#### 재고 경보")
        sa = LA.stock_alerts(stk9, kd9, k)
        last_sa = sa.iloc[-1]
        st.markdown(f"{theme.dot(last_sa['signal'])}**{sp9}** {last_sa['month']:%Y-%m} 재고는 **{last_sa['signal']}** ({vs_normal((last_sa['stock_kg'] / last_sa['expected_kg'] - 1) * 100)}). "
                    f"경보 기준은 사이드바의 '경보 민감도'({k})를 따릅니다.", unsafe_allow_html=True)
        sd9 = (sa["stock_kg"] - sa["expected_kg"]).std()
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=sa["month"], y=sa["expected_kg"] + k * sd9, line=dict(width=0), showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=sa["month"], y=sa["expected_kg"] - k * sd9, fill="tonexty", fillcolor="rgba(11,11,11,0.05)",
                                 line=dict(width=0), name="정상 범위", hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=sa["month"], y=sa["expected_kg"], name="평소 수준(추세·계절 반영)", line=dict(color=GRAY, dash="dot")))
        fig.add_trace(go.Scatter(x=sa["month"], y=sa["stock_kg"], name="실제 재고", mode="lines+markers", line=dict(color=ACTUAL, width=2)))
        bad9 = sa[sa["signal"] != "정상"]
        fig.add_trace(go.Scatter(x=bad9["month"], y=bad9["stock_kg"], mode="markers", name="경보", marker=dict(color=RED, size=10)))
        st.plotly_chart(layout(fig, f"{sp9} 월 재고 (kg)", 340), use_container_width=True)
        pa9 = LA.part_alerts(stk9, kd9, k)
        if len(pa9):
            t = pa9.assign(최신재고=(pa9["latest_kg"] / 1000).round(0), **{"전월(%)": pa9["mom_pct"].round(1), "평소 대비": pa9["z"].map(deviation_word)})
            st.markdown("**부위별 경보** (자기 이력 대비)")
            st.dataframe(t.rename(columns={"part": "부위", "signal": "신호"})[["부위", "최신재고", "전월(%)", "평소 대비", "신호"]].rename(columns={"최신재고": "최신 재고(톤)"}),
                         hide_index=True, use_container_width=True)
        st.caption("'평소 수준'은 최근 흐름(추세)과 계절 패턴을 반영해 계산한 값입니다. 재고 기록이 31개월뿐이라 단순한 방식을 썼고, 평소보다 벗어난 정도를 경보 기준으로 봅니다.")

        # ---------------- 가격 경보
        st.markdown("#### 가격 경보")
        gthr = st.slider("평년 대비 경보 기준 (±%)", 5, 30, 10, key="gap9")
        pal = LA.price_alerts(pdl9, pm9, live.KINDS, gthr)
        hot = pal[pal["alert"] != ""]
        st.markdown(f"경보 {len(hot)}건 / 전체 {len(pal)}개 부위")
        show = pal.assign(**{"최신가(원/kg)": pal["latest_krw_kg"].round(0), "평년 대비(%)": pal["gap_pct"].round(1), "30일(%)": pal["chg30_pct"].round(1)})
        st.dataframe(show.rename(columns={"item": "부위", "streak": "연속", "alert": "경보"})[["종류", "부위", "최신가(원/kg)", "평년 대비(%)", "30일(%)", "연속", "경보"]],
                     hide_index=True, use_container_width=True, height=300)

        # ---------------- 예측
        st.markdown("#### 예측과 정확도 확인")
        st.caption("예측 방법 5가지를 과거 데이터에 돌려 보고(일부를 가리고 맞혀 보기) 오차가 가장 작은 방법을 씁니다. **마지막 값을 그대로 쓰는 것보다 나아지지 않으면 '직전 값 그대로'가 선택됩니다.**")
        c1, c2 = st.columns(2)
        with c1:
            s_m = LA.monthly_stock(stk9, kd9)
            fm = LA.forecast_series(s_m, 3, 12, 3, 8)
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=s_m.index, y=s_m.values, name="실적", line=dict(color=ACTUAL, width=2)))
            fig.add_trace(go.Scatter(x=fm["index"], y=fm["hi"], line=dict(width=0), showlegend=False, hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=fm["index"], y=fm["lo"], fill="tonexty", fillcolor="rgba(82,81,78,0.10)", line=dict(width=0), name="참고 구간", hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=fm["index"], y=fm["pred"], name=f"예측({model_name(fm['best'])})", line=dict(color=MODEL, dash="dot", width=2), mode="lines+markers"))
            st.plotly_chart(layout(fig, f"{sp9} 월 재고 3개월 예측", 320), use_container_width=True)
            st.dataframe(_bt(fm["backtest"]), hide_index=True, use_container_width=True)
            if fm["improve_pct"] > 0:
                st.success(f"선택 모델이 직전값 기준선보다 오차 {fm['improve_pct']:.1f}% 감소")
            else:
                st.warning("기준선(직전값)보다 나은 모델이 없습니다. 이 구간의 예측은 '현 수준 유지'와 같습니다.")
        with c2:
            items9 = sorted(pdl9.loc[pdl9["kind"] == kd9, "item"].unique())
            main9 = "삼겹살" if species == "pork" else "등심"
            it9 = st.selectbox("소비자가 부위", items9, index=items9.index(main9) if main9 in items9 else 0, key="fc_item")
            s_d = LA.daily_price_series(pdl9, kd9, it9)
            fd = LA.forecast_series(s_d, 10, None, 7, 8, step=5)
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=s_d.index, y=s_d.values, name="실적", line=dict(color=ACTUAL, width=2)))
            fig.add_trace(go.Scatter(x=fd["index"], y=fd["hi"], line=dict(width=0), showlegend=False, hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=fd["index"], y=fd["lo"], fill="tonexty", fillcolor="rgba(82,81,78,0.10)", line=dict(width=0), name="참고 구간", hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=fd["index"], y=fd["pred"], name=f"예측({model_name(fd['best'])})", line=dict(color=MODEL, dash="dot", width=2)))
            fig.update_yaxes(title="원/kg")
            st.plotly_chart(layout(fig, f"{it9} 소비자가 10영업일 예측", 320), use_container_width=True)
            st.dataframe(_bt(fd["backtest"]), hide_index=True, use_container_width=True)
            if fd["improve_pct"] > 0:
                st.success(f"선택 모델이 직전값 기준선보다 오차 {fd['improve_pct']:.1f}% 감소")
            else:
                st.warning("기준선(직전값)보다 나은 모델이 없습니다.")

        # ---------------- 시차·상관
        st.markdown("#### 가격이 따라오는 데 걸리는 시간")
        if species == "pork":
            a_w = ap9[ap9["grade"] == "평균"].set_index("date")["price_krw_kg"]
            p_w = pdl9[(pdl9["kind"] == kd9) & (pdl9["item"] == "삼겹살")].groupby("date")["price_krw_kg"].mean()
            lbl = "제주 돼지 경락가 → 삼겹살 소비자가"
        else:
            a_w = ab9[ab9["cut"] == "등심"].set_index("date")["han_krw_kg"]
            p_w = pdl9[(pdl9["kind"] == kd9) & (pdl9["item"] == "등심")].groupby("date")["price_krw_kg"].mean()
            lbl = "한우 등심 경락가 → 등심 소비자가"
        ll = LA.lead_lag(a_w, p_w)
        c1, c2 = st.columns(2)
        with c1:
            if len(ll):
                fig = go.Figure(go.Bar(x=ll["lag"], y=ll["corr"], marker_color=[BLUE if p < 0.05 else GRAY for p in ll["p"]],
                                       text=[f"{c:+.2f}" for c in ll["corr"]], textposition="outside"))
                fig.update_xaxes(title="소비자가 반영 지연 (주)", dtick=1)
                st.plotly_chart(layout(fig, f"{lbl}: 몇 주 뒤에 따라가나", 300), use_container_width=True)
                sig_n = int((ll["p"] < 0.05).sum())
                note = " 뚜렷한 전달 시차는 확인되지 않았습니다." if sig_n <= 1 else ""
                st.caption(f"파란 막대는 우연으로 보기 어려운 관계입니다. 확인한 {len(ll)}개 시차 중 {sig_n}개만 그렇고, 데이터가 {int(ll['n'].min())}~{int(ll['n'].max())}주뿐이라 확정적이지 않습니다.{note}")
            else:
                st.info("표본이 부족합니다.")
        with c2:
            pm_m = pm9[(pm9["kind"] == kd9) & (pm9["item"] == main9)].groupby("month")["price_krw_kg"].mean()
            sp_c = LA.stock_price_corr(LA.monthly_stock(stk9, kd9), pm_m)
            if len(sp_c):
                fig = go.Figure(go.Bar(x=sp_c["lag_months"], y=sp_c["corr"], marker_color=[BLUE if p < 0.05 else GRAY for p in sp_c["p"]],
                                       text=[f"{c:+.2f}" for c in sp_c["corr"]], textposition="outside"))
                fig.update_xaxes(title="재고 변화 후 가격 반영 지연 (개월)", dtick=1)
                st.plotly_chart(layout(fig, f"{sp9} 재고가 바뀌면 {main9} 가격이 몇 달 뒤에 따라가나", 300), use_container_width=True)
                st.caption(f"데이터가 {int(sp_c['n'].min())}~{int(sp_c['n'].max())}개월뿐이라, 관계처럼 보여도 우연일 가능성이 큽니다.")
            else:
                st.info("표본이 부족합니다.")
        st.warning("재고 데이터는 월 단위이고 약 2~3개월 지연되어 있어, 오늘의 재고 상황이 아니라 마지막 집계 월 기준입니다.")

        # ---------------- 알림 이력
        st.markdown("#### 알림 이력")
        hist = alerts.load_history(n=40)
        if hist:
            st.caption("수집 직후 규칙을 평가해 새로 생긴 경보만 기록합니다 (같은 경보는 다시 보내지 않음). Windows 알림은 `collect.py` 실행 시 발송됩니다.")
            st.dataframe(pd.DataFrame(hist).rename(columns={"at": "시각", "level": "등급", "rule": "규칙", "message": "내용"})[["시각", "등급", "규칙", "내용"]],
                         hide_index=True, use_container_width=True, height=260)
        else:
            st.info("아직 발송된 경보가 없습니다. `python -m lib.alerts` 로 지금 평가해 볼 수 있습니다.")


def page_live():
    st.subheader("실제 데이터 (축산물품질평가원 OpenAPI)")
    st.caption("'시연 (합성 데이터)' 화면은 합성 CSV이고, 이 화면은 실제 API 데이터입니다. 규모가 달라 서로 비교하지 마세요 "
               "(예: 실제 돼지 재고 약 1.8만 톤, 합성 약 9.2만 톤).")
    if not live.has_key():
        st.error("`.env` 에 인증키(KEY_EKAPE_DISTRIB)가 없습니다.")
        st.stop()
    LD = live_data(_live_sig())
    pdl = LD["price_daily"]
    c1, c4 = st.columns([4, 1])
    c1.markdown(
        f"**마지막 갱신** {live.last_refresh() or '없음'} · **오늘 API 호출** {live.calls_today()}/{live.DAILY_BUDGET} "
        f"(개발계정 하루 1,000건 제한) · **가격 데이터** "
        + (f"{pdl['date'].min():%Y-%m-%d} ~ {pdl['date'].max():%Y-%m-%d}" if len(pdl) else "-"))
    with c4:
        if st.button("지금 갱신 (최근 데이터)", use_container_width=True):
            bar = st.progress(0.0, text="API 호출 중...")
            res = live.refresh(days_back=10, progress=lambda i, n: bar.progress(i / max(n, 1), text=f"{i}/{n}"))
            bar.empty()
            live.quality_check()
            st.cache_data.clear()
            if res["errors"]:
                st.warning(f"일부 실패 {len(res['errors'])}건: {res['errors'][0]}")
            st.rerun()
    if pdl.empty:
        st.info("아직 수집된 데이터가 없습니다. 터미널에서 `python -m lib.live 60` 을 실행하거나 위 버튼을 누르세요.")
        st.stop()

    status, issues, errs = live.last_status(), live.load_quality(), live.recent_errors()
    warn_n = sum(1 for i in issues if i["level"] == "경고")
    if status and not status.get("ok", True):
        st.error(f"마지막 수집에서 {status['errors']}건 실패했습니다. 아래 '수집 상태·품질'을 확인하세요.")
    elif status.get("pending"):
        st.info(f"호출 한도 때문에 과거 {status['pending']}건이 대기 중입니다. 다음 수집에서 이어서 받습니다.")
    with st.expander(f"수집 상태·품질 (품질 경고 {warn_n}건, 최근 실패 {len(errs)}건)"):
        if status:
            st.write(f"마지막 수집 {status['at']} · 호출 {status['calls']}건 · 실패 {status['errors']}건 · 대기 {status['pending']}건")
        if issues:
            st.dataframe(pd.DataFrame(issues).rename(columns={"level": "등급", "table": "테이블", "when": "시점", "detail": "내용"}),
                         hide_index=True, use_container_width=True)
        else:
            st.write("품질 점검에서 발견된 이상이 없습니다.")
        if errs:
            st.code("\n".join(errs), language=None)

    kd, ki = KIND[species], KIND_IMP[species]
    stk, pm = LD["stock"], LD["price_month"]
    ab, ap = LD["auction_beef"], LD["auction_pig"]
    live_notes = []

    def latest_and_prev(df, mask, days=7):
        d = df[mask].sort_values("date")
        if d.empty:
            return None, None
        cur = d.iloc[-1]
        prev = d[d["date"] <= cur["date"] - pd.Timedelta(days=days)]
        return cur, (prev.iloc[-1] if len(prev) else None)

    # ---- KPI
    k1, k2, k3, k4, k5 = st.columns(5)
    main_item = "삼겹살" if species == "pork" else "등심"
    cur, prv = latest_and_prev(pdl, (pdl["kind"] == kd) & (pdl["item"] == main_item))
    if cur is not None:
        k1.metric(f"{sp_ko} {main_item} 소비자가", f"{cur['price_krw_kg']:,.0f}원/kg",
                  f"{(cur['price_krw_kg'] / prv['price_krw_kg'] - 1) * 100:+.1f}% 1주 전" if prv is not None else None,
                  delta_color="inverse", help=f"{cur['date']:%Y-%m-%d} · 등급 {cur['grade']}")
    mm = pm[(pm["kind"] == kd) & (pm["item"] == main_item)]
    if len(mm):
        lm = mm[mm["month"] == mm["month"].max()]
        gap = (lm["price_krw_kg"].mean() / lm["normal_krw_kg"].mean() - 1) * 100
        k2.metric("평년 대비 (이번 달)", f"{gap:+.1f}%", delta_color="off", help="월평균 소비자가 ÷ 평년가격")
        live_notes.append(("warn" if abs(gap) >= 5 else "ok", f"{sp_ko} {main_item} 소비자가는 평년 대비 **{gap:+.1f}%** 입니다."))
    sk = stk[(stk["kind"] == kd) & (stk["part"] == "합계")].sort_values("month")
    mom = np.nan
    if len(sk) >= 2:
        mom = (sk["stock_kg"].iloc[-1] / sk["stock_kg"].iloc[-2] - 1) * 100
        k3.metric(f"{sp_ko} 재고 ({sk['month'].iloc[-1]:%Y-%m})", fmt_kg(sk["stock_kg"].iloc[-1]), f"{mom:+.1f}% 전월", delta_color="off")
    if species == "beef" and len(ab):
        a = ab[ab["cut"] == "소계"].sort_values("date")
        if len(a):
            k4.metric("한우 소계 경락가", f"{a['han_krw_kg'].iloc[-1]:,.0f}원/kg", help=f"{a['date'].iloc[-1]:%Y-%m-%d}, 전 도매시장 평균")
    elif species == "pork" and len(ap):
        a = ap[ap["grade"] == "평균"].sort_values("date")
        if len(a):
            k4.metric("제주 돼지 경락가", f"{a['price_krw_kg'].iloc[-1]:,.0f}원/kg", help=f"{a['date'].iloc[-1]:%Y-%m-%d}, 지육 기준")
    if len(sk) >= 12:  # 실제 재고 기준 신호 ('실제 분석·예측' 화면과 같은 방법: 추세+계절 기대값 대비 z, 민감도는 사이드바)
        _sa = LA.stock_alerts(stk, kd, k).iloc[-1]
        k5.markdown(theme.kpi_html(f"{sp_ko} 재고 신호 ({_sa['month']:%Y-%m})", VERDICT[_sa["signal"]],
                                   vs_normal((_sa["stock_kg"] / _sa["expected_kg"] - 1) * 100), tone=_sa["signal"],
                                   help="실제 월 재고가 평소 수준(추세·계절 반영)과 얼마나 다른지로 판단합니다. 기준은 사이드바의 경보 민감도를 따릅니다. 마지막 집계 월 기준입니다."),
                    unsafe_allow_html=True)

    # ---- 재고
    st.markdown("#### 부위별 재고 (월)")
    if len(sk) >= 6:
        mu, sdv = sk["stock_kg"].mean(), sk["stock_kg"].std()
        sk = sk.assign(z=(sk["stock_kg"] - mu) / sdv)
        l, r = st.columns(2)
        with l:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=sk["month"], y=[mu + k * sdv] * len(sk), line=dict(width=0), showlegend=False, hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=sk["month"], y=[mu - k * sdv] * len(sk), fill="tonexty", fillcolor="rgba(11,11,11,0.05)",
                                     line=dict(width=0), name="정상 범위", hoverinfo="skip"))
            fig.add_trace(go.Scatter(x=sk["month"], y=sk["stock_kg"], name="재고", line=dict(color=ACTUAL, width=2), mode="lines+markers"))
            bad = sk[sk["z"].abs() >= k]
            fig.add_trace(go.Scatter(x=bad["month"], y=bad["stock_kg"], mode="markers", name="경보", marker=dict(color=RED, size=10)))
            st.plotly_chart(layout(fig, f"{sp_ko} 재고 합계 (kg)", 340), use_container_width=True)
        with r:
            parts = stk[(stk["kind"] == kd) & (stk["part"] != "합계") & (stk["month"] >= stk["month"].max() - pd.DateOffset(months=11))]
            fig = go.Figure()
            for p, g in parts.groupby("part"):
                fig.add_trace(go.Bar(x=g["month"], y=g["stock_kg"], name=p))
            fig.update_layout(barmode="stack")
            st.plotly_chart(layout(fig, "부위별 재고 (최근 12개월)", 340), use_container_width=True)
        zl = sk.iloc[-1]["z"]
        sig = "부족" if zl <= -k else "과잉" if zl >= k else "정상"
        live_notes.append(("bad" if sig != "정상" else "ok", f"{sp_ko} 실제 재고({sk['month'].iloc[-1]:%Y-%m})는 **{sig}** 구간입니다 ({vs_normal((sk.iloc[-1]['stock_kg'] / mu - 1) * 100).replace('평소보다', '전체 평균보다')}, 전월 {mom:+.1f}%)."))
        lastp = parts[parts["month"] == parts["month"].max()]
        top = lastp.sort_values("stock_kg", ascending=False).iloc[0]
        live_notes.append(("ok", f"최근 재고가 가장 많은 부위는 **{top['part']}** ({top['stock_kg'] / lastp['stock_kg'].sum() * 100:.0f}%) 입니다."))
        st.caption("재고는 월 단위이고 가장 최근 월이 약 2~3개월 전입니다. 돼지는 톤 단위를 kg으로 환산했습니다.")

    # ---- 소비자가격
    st.markdown("#### 소비자가격")
    items = sorted(pdl.loc[pdl["kind"] == kd, "item"].unique())
    cc1, cc2 = st.columns(2)
    item = cc1.selectbox("부위", items, index=items.index(main_item) if main_item in items else 0, key="live_item")
    grades = sorted(pdl.loc[(pdl["kind"] == kd) & (pdl["item"] == item), "grade"].unique())
    grade = cc2.selectbox("등급", grades, key="live_grade")
    sel = pdl[(pdl["kind"] == kd) & (pdl["item"] == item) & (pdl["grade"] == grade)].sort_values("date")
    l, r = st.columns(2)
    with l:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=sel["date"], y=sel["max_krw_kg"], line=dict(width=0), showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=sel["date"], y=sel["min_krw_kg"], fill="tonexty", fillcolor="rgba(42,120,214,0.12)",
                                 line=dict(width=0), name="지역 최저~최고", hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=sel["date"], y=sel["price_krw_kg"], name="평균", line=dict(color=ACTUAL, width=2)))
        fig.update_yaxes(title="원/kg")
        st.plotly_chart(layout(fig, f"{item} 일별 소비자가", 340), use_container_width=True)
    with r:
        mi = pm[(pm["kind"] == kd) & (pm["item"] == item)]
        fig = go.Figure()
        for g, d in mi.groupby("grade"):
            fig.add_trace(go.Scatter(x=d["month"], y=(d["price_krw_kg"] / d["normal_krw_kg"] - 1) * 100, name=f"등급 {g}"))
        fig.add_hline(y=0, line_dash="dot", line_color=GRAY)
        fig.update_yaxes(title="평년 대비 %")
        st.plotly_chart(layout(fig, f"{item} 월별 평년 대비", 340), use_container_width=True)

    # ---- 국산 vs 수입 가격 격차
    pair = ("삼겹살", "삼겹살") if species == "pork" else ("갈비", "갈비(냉동)")
    dom = pdl[(pdl["kind"] == kd) & (pdl["item"] == pair[0])].groupby("date")["price_krw_kg"].mean()
    imp_ = pdl[(pdl["kind"] == ki) & (pdl["item"] == pair[1])].groupby("date")["price_krw_kg"].mean()
    both = pd.concat([dom.rename("국산"), imp_.rename("수입")], axis=1).dropna()
    if len(both):
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=both.index, y=both["국산"], name=f"국산 {pair[0]}", line=dict(color=ACTUAL)))
        fig.add_trace(go.Scatter(x=both.index, y=both["수입"], name=f"수입 {pair[1]}", line=dict(color=FILEFC)))
        st.plotly_chart(layout(fig, "국산 vs 수입 소비자가 (원/kg)", 300), use_container_width=True)
        ratio = both["국산"].iloc[-1] / both["수입"].iloc[-1]
        live_notes.append(("ok", f"국산 {pair[0]}은 수입 {pair[1]}의 **{ratio:.2f}배** 가격입니다 (격차가 클수록 수입 수요 유인이 큽니다)."))

    # ---- 경락가격
    st.markdown("#### 경락가격 (도매 단계)")
    if species == "beef":
        cuts = [c for c in sorted(ab["cut"].unique()) if c not in ("소계", "잡뼈", "꼬리", "사골", "우족")]
        cut = st.selectbox("부위 ", cuts, index=cuts.index("등심") if "등심" in cuts else 0, key="live_cut")
        a = ab[ab["cut"] == cut].sort_values("date")
        fig = go.Figure()
        fig.add_trace(go.Bar(x=a["date"], y=a["han_boxes"], name="한우 거래(박스)", marker_color="rgba(137,135,129,0.35)", yaxis="y2"))
        fig.add_trace(go.Scatter(x=a["date"], y=a["han_krw_kg"], name="한우 경락가", line=dict(color=ACTUAL, width=2)))
        fig.add_trace(go.Scatter(x=a["date"], y=a["yuk_krw_kg"], name="육우 경락가", line=dict(color=FILEFC, width=2)))
        fig.update_layout(yaxis2=dict(overlaying="y", side="right", showgrid=False, title="박스"))
        st.plotly_chart(layout(fig, f"소 {cut} 경락가 (원/kg, 전 도매시장 평균)", 340), use_container_width=True)
    else:
        a = ap[ap["grade"] == "평균"].sort_values("date")
        fig = go.Figure()
        fig.add_trace(go.Bar(x=a["date"], y=a["heads"], name="거래 두수", marker_color="rgba(137,135,129,0.35)", yaxis="y2"))
        fig.add_trace(go.Scatter(x=a["date"], y=a["price_krw_kg"], name="평균 경락가", line=dict(color=ACTUAL, width=2)))
        fig.update_layout(yaxis2=dict(overlaying="y", side="right", showgrid=False, title="두수"))
        st.plotly_chart(layout(fig, "제주 돼지도체 경락가 (원/kg, 지육)", 340), use_container_width=True)
        st.caption("돼지 경락가는 제주 지역만 제공됩니다. 전국 시세의 대표값으로 쓰기에는 한계가 있습니다.")

    # ---- 인사이트
    st.markdown("#### 자동 인사이트 (실제 데이터)")
    for lvl, txt in live_notes:
        st.markdown(f"{theme.dot(lvl)}{txt}", unsafe_allow_html=True)


# ------------------------------------------------------------------ 왼쪽 내비게이션
_nav = st.navigation({
    "오늘": [st.Page(page_today, title="오늘의 현황", url_path="today", default=True)],
    "우리 공장": [st.Page(page_company, title="우리 공장", url_path="company")],
    "실제 데이터": [st.Page(page_live, title="실제 데이터", url_path="live"),
                 st.Page(page_analysis, title="실제 분석·예측", url_path="analysis")],
    "시연 (합성 데이터)": [st.Page(page_syn_status, title="현황·경보", url_path="demo-status"),
                      st.Page(page_syn_trend, title="수급 추이·예측", url_path="demo-trend"),
                      st.Page(page_syn_season, title="계절성·이상치", url_path="demo-season"),
                      st.Page(page_syn_event, title="명절·이벤트", url_path="demo-event"),
                      st.Page(page_syn_import, title="수입국·단가", url_path="demo-import"),
                      st.Page(page_syn_price, title="가격·소비", url_path="demo-price"),
                      st.Page(page_syn_flow, title="유통 병목", url_path="demo-flow")],
}, position="sidebar")
_nav.run()
