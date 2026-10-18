"""원가 영향 화면: 로트 단가 → 제품 kg당 원료비 → 단가 변동 가정 → 계획 기간 원료비·발주 금액."""
from __future__ import annotations

import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from lib import advice as A
from lib import company as C
from lib import public_demo
from lib import cost as K
from lib import theme
from lib.plain import change_text, md, ton, won, won_big

DAYS = 14


def _pct(v: float) -> str:
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:+.1f}%"


def _ratio(v: float) -> str:
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.1f}%"


def _material_colors(names: list[str]) -> dict[str, str]:
    """원료마다 고정 색. 8개를 넘으면 나머지는 회색 '기타'로 묶는다(색을 순환시키지 않음)."""
    keep = names if len(names) <= len(theme.SERIES) else names[: len(theme.SERIES) - 1]
    return {n: theme.SERIES[i] for i, n in enumerate(sorted(keep))}  # 이름순으로 색을 고정해 단가 기준을 바꿔도 색이 유지된다


def _assumption_editor(data: C.CompanyData, prices: pd.DataFrame, basis: str, market: A.Market) -> dict[str, float]:
    """원료별 단가 변동률(%) 입력. 시장 소비자가 변화율로 한 번에 채울 수 있다."""
    ver = st.session_state.setdefault("cost_ver", 0)
    saved: dict = st.session_state.setdefault("cost_pct", {})
    source = st.radio("자동 채우기 기준", list(K.SOURCES), format_func=K.SOURCES.get, horizontal=True, key="cost_source",
                      help="경락가(국산)는 도매시장 가격이라 소비자가보다 매입가 흐름에 가깝습니다(돼지는 제주 도체 평균 하나뿐). 환율(수입)은 USD 매매기준율 30일 변화 × 전가율을 수입 원료 단가 변화로 봅니다.")
    from lib import ui_company as UC
    mk = K.source_changes(data, market, source, UC.get_settings())
    price = K.unit_price(prices, basis)
    used = sorted(set(K.components(data)["원료"]))
    rows = [{"원료": n, "기준 단가(원/kg)": price.get(n, (math.nan, ""))[0],
             "시장 30일 변화": f"{mk[n][1]} {mk[n][0]:+.1f}%" if n in mk else "연결 시세 없음",
             "단가 변동 가정(%)": float(saved.get(n, 0.0))} for n in used]
    b1, b2, _ = st.columns([2.2, 1.2, 4])
    if b1.button("시장 30일 변화율로 채우기", disabled=not mk, width="stretch",
                 help="선택한 기준의 최근 30일 변화율을 그대로 매입 단가 변화로 가정합니다. 연결 시세가 없는 원료는 0%로 둡니다. 실제 매입가 변화와 다를 수 있습니다."):
        st.session_state["cost_pct"] = {n: v for n, (v, _) in mk.items()}
        st.session_state["cost_ver"] = ver + 1
        st.rerun()
    if b2.button("가정 초기화", width="stretch"):
        st.session_state["cost_pct"] = {}
        st.session_state["cost_ver"] = ver + 1
        st.rerun()
    edited = st.data_editor(pd.DataFrame(rows), hide_index=True, width="stretch", key=f"cost_editor_{ver}",
                            disabled=["원료", "기준 단가(원/kg)", "시장 30일 변화"],
                            column_config={"기준 단가(원/kg)": st.column_config.NumberColumn(format="%,.0f"),
                                           "단가 변동 가정(%)": st.column_config.NumberColumn(min_value=-50.0, max_value=100.0, step=0.5, format="%+.1f")})
    pct = {r["원료"]: float(r["단가 변동 가정(%)"] or 0.0) for _, r in edited.iterrows()}
    st.session_state["cost_pct"] = pct
    if not mk:
        st.caption("수집된 시장 데이터가 없거나 쓰는 원료와 연결되는 시세가 없어 자동 채우기를 쓸 수 없습니다.")
    st.caption("경락가·소비자가 모두 우리 매입 단가가 아닙니다. 경락가는 최근 5거래일 평균을 30일 전 무렵 5거래일 평균과 비교합니다. "
               "방향과 크기를 가늠하는 가정으로만 쓰세요.")
    return pct


def _cost_chart(comp: pd.DataFrame, prod: pd.DataFrame, layout) -> go.Figure:
    order = prod.sort_values("원료비_원kg", na_position="first")["제품"].tolist()
    totals = comp.groupby("원료")["원료비"].sum().sort_values(ascending=False)
    names = totals.index.tolist()
    colors = _material_colors(names)
    c = comp.copy()
    c["원료"] = c["원료"].where(c["원료"].isin(colors), "기타")
    fig = go.Figure()
    for n in [*colors, "기타"]:
        g = c[c["원료"] == n].groupby("제품")["원료비"].sum().reindex(order)
        if g.notna().any() and g.sum() > 0:
            fig.add_trace(go.Bar(y=order, x=g.values, name=n, orientation="h",
                                 marker_color=colors.get(n, theme.MUTED), hovertemplate="%{y} · " + n + ": %{x:,.0f}원/kg<extra></extra>"))
    p = prod.set_index("제품").reindex(order)
    if (p["부산물회수_원kg"] > 0.5).any():
        fig.add_trace(go.Scatter(y=order, x=p["원료비_원kg"], mode="markers", name="부산물 뺀 순원료비",
                                 marker=dict(symbol="circle-open", size=10, color=theme.INK, line=dict(width=2)),
                                 hovertemplate="%{y} 순원료비: %{x:,.0f}원/kg<extra></extra>"))
    if (p["변화_원kg"].abs() > 0.5).any():
        fig.add_trace(go.Scatter(y=order, x=p["가정원료비_원kg"], mode="markers", name="가정 적용 후",
                                 marker=dict(symbol="diamond", size=11, color=theme.INK, line=dict(color=theme.SURFACE, width=1)),
                                 hovertemplate="%{y} 가정 후: %{x:,.0f}원/kg<extra></extra>"))
    if p["판매가_원kg"].notna().any():
        fig.add_trace(go.Scatter(y=order, x=p["판매가_원kg"], mode="markers", name="판매가",
                                 marker=dict(symbol="line-ns", size=18, line=dict(color=theme.INK2, width=2)),
                                 hovertemplate="%{y} 판매가: %{x:,.0f}원/kg<extra></extra>"))
    fig.update_layout(barmode="stack", hovermode="closest")
    fig.update_xaxes(title="원/kg (제품 1kg당)", tickformat=",.0f", showgrid=True, gridcolor=theme.GRID)
    fig.update_yaxes(showgrid=False)
    return layout(fig, "제품 1kg당 원료비 구성", max(260, 70 + 46 * len(order)))


def _assumption_store(label: str, basis: str, pct: dict, cur_total: float, new_total: float, r0: float, r1: float) -> None:
    """가정에 이름을 붙여 저장·불러오기, 조치 이력에 연결. 샘플은 이 브라우저 세션에만 남는다."""
    sample = label.startswith("샘플")
    items = st.session_state.get("sample_assumptions", []) if sample else K.load_assumptions()
    with st.expander(f"가정 저장·불러오기 ({len(items)}개 저장됨)"):
        a, b = st.columns([3, 1])
        name = a.text_input("가정 이름", key="cost_assume_name", placeholder="예: 10월 원가회의 — 환율 +5%")
        changed = {k: v for k, v in pct.items() if abs(v) >= 0.05}
        entry = {"이름": name.strip(), "저장시각": str(pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).floor("s")), "출처": label,
                 "단가기준": K.BASIS.get(basis, basis), "자동채우기기준": K.SOURCES.get(st.session_state.get("cost_source", ""), ""),
                 "가정": changed, "계획기간원료비_현재": round(cur_total), "계획기간원료비_가정": round(new_total),
                 "원료비율_현재": None if r0 != r0 else round(r0, 2), "원료비율_가정": None if r1 != r1 else round(r1, 2)}
        if b.button("저장", key="cost_assume_save", width="stretch", disabled=not name.strip()):
            if sample:
                st.session_state["sample_assumptions"] = [entry, *[e for e in items if e["이름"] != entry["이름"]]][:K.MAX_ASSUMPTIONS]
            else:
                K.save_assumption(entry)
            st.success(f"'{entry['이름']}'을 저장했습니다." + (" 샘플 가정은 이 세션에만 남습니다." if sample else ""))
            items = st.session_state.get("sample_assumptions", []) if sample else K.load_assumptions()
        if items:
            names = [e["이름"] for e in items]
            pick = st.selectbox("저장한 가정", names, key="cost_assume_pick",
                                format_func=lambda n: next(f"{e['이름']} · {e['저장시각'][:16]} · 변화 {won_big(e['계획기간원료비_가정'] - e['계획기간원료비_현재'])}"
                                                            for e in items if e["이름"] == n))
            chosen = next(e for e in items if e["이름"] == pick)
            st.caption("원료별 가정: " + (", ".join(f"{k} {v:+.1f}%" for k, v in chosen["가정"].items()) or "변화 없음")
                       + f" · 단가 기준 {chosen['단가기준']}")
            x, y = st.columns(2)
            if x.button("이 가정 불러오기", key="cost_assume_load", width="stretch"):
                st.session_state["cost_pct"] = dict(chosen["가정"])
                st.session_state["cost_ver"] = st.session_state.get("cost_ver", 0) + 1
                st.rerun()
            if y.button("조치 이력에 연결", key="cost_assume_link", width="stretch"):
                st.session_state["pending_action"] = {"출처": label, "판단일": str(C.today_kst().date()), "대응안": f"원가 가정: {chosen['이름']}",
                                                      "원가가정": chosen}
                st.success("'조치 이력' 화면에서 담당자·기한·협의 내용을 적어 저장하세요. 가정과 계산 결과가 근거로 함께 남습니다.")
        st.caption("가정은 화면 계산에만 쓰이며 CSV·발주·자동 알림에는 반영되지 않습니다. 불러오면 지금 데이터로 다시 계산합니다(저장 당시 금액과 다를 수 있음).")


def today_summary(data: C.CompanyData) -> None:
    """'오늘의 생산 위험'용 한 줄: 계획 기간 원료비와, 경락가 최근 30일 흐름이 이어질 때의 변화."""
    if not K.has_prices(data):
        return
    from lib import ui_company as UC
    settings = UC.get_settings()
    fxc = K.fx_changes(data, settings)
    chg = {n: v for n, (v, _) in {**K.auction_changes(data), **fxc}.items()}
    prod = K.product_costs(data, "avg", chg, settings)
    ok = prod[prod["단가없는원료"] == ""]
    if not len(ok):
        return
    cur, new = float(ok["계획원료비"].sum()), float(ok["가정계획원료비"].sum())
    parts = [f"계획 기간 원료비 **{won_big(cur)}**"]
    if chg and abs(new - cur) >= 1:
        what = "국산 경락가" + (f"·수입 USD 환율({next(iter(fxc.values()))[1].split(') ')[1].split(' ×')[0]})" if fxc else "")
        parts.append(f"{what} 최근 30일 흐름이 매입가에 그대로 붙으면 **{'+' if new > cur else ''}{won_big(new - cur)}** ({_pct((new / cur - 1) * 100)})")
        s = ok[ok["판매가_원kg"].notna()]
        sales = float((s["판매가_원kg"] * s["계획물량_kg"]).sum())
        if sales:
            parts.append(f"원료비율 {_ratio(float(s['계획원료비'].sum()) / sales * 100)} → {_ratio(float(s['가정계획원료비'].sum()) / sales * 100)}")
    with st.container(border=True):
        st.markdown("**원가** · " + " · ".join(parts))
        st.caption("가정 계산입니다(경락가는 매입가가 아님. 수입 원료는 USD 환율 변화" + ("" if fxc else "가 없어 0%로 둠") + "). 원료별 가정 조정은 '원가 영향' 화면에서 합니다." +
                   (" 단가가 없는 원료가 들어간 제품은 합계에서 뺐습니다." if len(ok) < len(prod) else ""))


def render_cost(k: float, layout) -> None:
    from lib import ui_company as UC
    st.subheader("원가 영향")
    st.caption("재고·입고 예정 CSV의 **단가_원kg**과 배합표로 제품 1kg당 **원료비**를 계산하고, 단가가 오르내리면 원료비와 원료비율이 얼마나 바뀌는지 봅니다. "
               "가공비·포장재·인건비·물류비는 포함하지 않습니다.")
    if UC.render_lock("원가 영향"):
        return
    left, right, _ = st.columns([2, 2, 4])
    if left.button("샘플(가짜) 데이터로 시험", key="cost_sample", width="stretch"):
        st.session_state["co_source"] = "sample"
        st.rerun()
    if not public_demo.enabled() and right.button("올려 둔 파일 사용", key="cost_saved", width="stretch"):
        st.session_state["co_source"] = "saved"
        st.rerun()
    data, label = UC.current_company()
    if data is None:
        st.info("샘플을 선택하거나 '원료 소요·재고'에서 CSV를 올려 주세요.")
        return
    if not data.ready():
        st.error("계산 불완전: '원료 소요·재고' 화면의 입력 오류를 먼저 수정해 주세요.")
        return
    if not K.has_prices(data):
        st.info("단가가 없습니다. **재고.csv**와 **입고예정.csv**에 `단가_원kg` 열(원/kg)을 추가하면 계산합니다. "
                "생산계획.csv에 `판매가_원kg`를 넣으면 원료비율도 보여 줍니다. 모르는 칸은 비워 두세요(0원으로 계산하지 않습니다).")
        return
    stale = C.freshness(data, C.today_kst())
    st.caption(f"{label} · 재고 기준일 {md(data.inventory['기준일'].max())}")
    if stale:
        st.warning(stale + " 원료비 계산은 단가 기준이라 보여 주지만, 발주 금액은 표시하지 않습니다.")

    prices = K.material_prices(data)
    st.markdown("#### 1. 원료 단가")
    basis = st.radio("어떤 단가로 볼까요", list(K.BASIS), format_func=K.BASIS.get, horizontal=True, key="cost_basis",
                     help="장부 기준: 지금 가진 재고와 입고 예정 로트의 수량 가중 평균. 재구매 기준: 가장 늦은 입고 예정 로트의 단가(다음에 살 때 값에 가깝습니다).")
    pt = prices.assign(**{"평균 단가": prices["평균단가"].map(won), "입고 예정 단가": prices["입고예정단가"].map(won),
                          "단가 적힌 수량 비율": prices["단가입력비율"].map(lambda v: f"{v * 100:.0f}%")})
    st.dataframe(pt[["원료", "평균 단가", "입고 예정 단가", "단가 적힌 수량 비율"]], hide_index=True, width="stretch")
    partial = prices[(prices["단가입력비율"] < 0.999) & prices["평균단가"].notna()]["원료"].tolist()
    if partial:
        st.caption("일부 로트에만 단가가 있는 원료는 단가가 적힌 로트로만 평균을 냈습니다: " + ", ".join(partial))

    st.markdown("#### 2. 단가 변동 가정")
    try:
        market = UC.market_cached(UC.live_sig(), k)
    except Exception:
        market = A.Market()
    pct = _assumption_editor(data, prices, basis, market)

    settings = UC.get_settings()
    prod = K.product_costs(data, basis, pct, settings)
    comp = K.components(data, basis, pct, settings)
    st.markdown("#### 3. 제품별 원료비")
    ok = prod[prod["단가없는원료"] == ""]
    if len(ok) < len(prod):
        st.warning("단가가 없는 원료가 들어가 원료비를 계산하지 못한 제품: " +
                   "; ".join(f"{r['제품']}({r['단가없는원료']})" for _, r in prod[prod["단가없는원료"] != ""].iterrows()))
    cur_total, new_total = float(ok["계획원료비"].sum()), float(ok["가정계획원료비"].sum())
    has_sale = K.has_sales(data) and ok["판매가_원kg"].notna().any()
    c1, c2, c3 = st.columns(3)
    back_total = float((ok["부산물회수_원kg"] * ok["계획물량_kg"]).sum())
    c1.markdown(theme.kpi_html("계획 기간 원료비" + (" (순)" if back_total > 0.5 else ""), won_big(cur_total),
                               f"명시된 생산계획 {ton(float(ok['계획물량_kg'].sum()))} 기준" + (f" · 부산물 회수 {won_big(back_total)} 뺌" if back_total > 0.5 else "")), unsafe_allow_html=True)
    diff = new_total - cur_total
    c2.markdown(theme.kpi_html("가정 적용 시 변화", ("+" if diff > 0 else "") + won_big(diff) if abs(diff) >= 1 else "변화 없음",
                               _pct((new_total / cur_total - 1) * 100) if cur_total and abs(diff) >= 1 else "단가 변동 가정을 넣으면 바뀝니다",
                               tone="critical" if diff > 0.5 else None), unsafe_allow_html=True)
    if has_sale:
        s = ok[ok["판매가_원kg"].notna()]
        sales = float((s["판매가_원kg"] * s["계획물량_kg"]).sum())
        r0 = float(s["계획원료비"].sum()) / sales * 100 if sales else math.nan
        r1 = float(s["가정계획원료비"].sum()) / sales * 100 if sales else math.nan
        c3.markdown(theme.kpi_html("원료비율 (판매가 대비)", _ratio(r1) if abs(r1 - r0) >= 0.05 else _ratio(r0),
                                   f"현재 {_ratio(r0)} → 가정 {_ratio(r1)}" if abs(r1 - r0) >= 0.05 else "판매가가 적힌 제품 기준"), unsafe_allow_html=True)
    else:
        c3.markdown(theme.kpi_html("원료비율 (판매가 대비)", "-", "생산계획에 판매가_원kg가 없음"), unsafe_allow_html=True)
    _assumption_store(label, basis, pct, cur_total, new_total, r0 if has_sale else math.nan, r1 if has_sale else math.nan)

    has_by = bool((prod["부산물회수_원kg"] > 0.5).any())
    table = pd.DataFrame({
        "제품": prod["제품"], **({"총원료비": prod["총원료비_원kg"].map(won), "부산물 회수": prod["부산물회수_원kg"].map(lambda v: f"-{v:,.0f}원" if v > 0.5 else "-")} if has_by else {}),
        ("순원료비(kg당)" if has_by else "kg당 원료비"): prod["원료비_원kg"].map(won), "가정 후": prod["가정원료비_원kg"].map(won),
        "변화": prod.apply(lambda r: "-" if math.isnan(r["변화_원kg"]) else f"{r['변화_원kg']:+,.0f}원 ({r['변화율']:+.1f}%)", axis=1),
        "판매가": prod["판매가_원kg"].map(won), "원료비율": prod["원료비율"].map(_ratio), "가정 후 원료비율": prod["가정원료비율"].map(_ratio),
        "계획 물량": prod["계획물량_kg"].map(ton), "계획 기간 원료비 변화": (prod["가정계획원료비"] - prod["계획원료비"]).map(lambda v: "-" if math.isnan(v) else ("+" if v > 0 else "") + won_big(v)),
    })
    st.dataframe(table, hide_index=True, width="stretch")
    if len(ok):
        st.plotly_chart(_cost_chart(comp[comp["제품"].isin(ok["제품"])], ok, layout), width="stretch")
        st.caption("막대는 현재 단가 기준 원료별 원료비입니다. ◆는 단가 변동 가정을 적용한 원료비, 세로 막대(|)는 판매가입니다."
                   + (" ○는 부산물 회수를 뺀 순원료비이며, 원료비율·계획 기간 원료비·변화는 모두 순원료비 기준입니다." if has_by else ""))
        if not has_by:
            st.caption("부산물.csv(원료별 부산물률·부산물 단가)를 올리면 손질 때 나오는 지방·뼈 등의 회수액을 빼고 순원료비로 계산합니다.")
    with st.expander("제품별 원료 구성 자세히"):
        d = comp.assign(**{"투입량(제품 1kg당)": comp["투입량_kg"].map(lambda v: f"{v:.2f}kg"), "단가 ": comp["단가"].map(won), "해동 감량": comp["해동감량률"].map(lambda v: f"{v:.1f}%" if v else "-"),
                           "가정 단가": comp["가정단가"].map(won), "원료비 ": comp["원료비"].map(won), "가정 원료비": comp["가정원료비"].map(won)})
        st.dataframe(d[["제품", "원료", "투입량(제품 1kg당)", "단가 ", "해동 감량", "가정 단가", "원료비 ", "가정 원료비", "근거"]], hide_index=True, width="stretch")
        st.caption("냉동 원료는 해동 후 무게로 배합하므로, 원료비 = 투입량 × 단가 ÷ (1 − 해동 감량률)입니다. 가정 단가도 해동 후 기준입니다.")

    st.markdown("#### 4. 원료별 매입 금액")
    proj = C.project(data, today=C.today_kst(), settings=UC.get_settings())
    amt = K.material_amounts(data, proj, "next", pct, DAYS, settings)
    at = pd.DataFrame({"원료": amt["원료"], "적용 단가": amt["적용단가"].map(won), "단가 근거": amt["근거"],
                       f"앞으로 {DAYS}일 소요": amt[f"{DAYS}일소요_kg"].map(ton), f"{DAYS}일 소요 금액": amt[f"{DAYS}일소요금액"].map(won_big),
                       "발주 검토량": amt["발주검토량_kg"].map(lambda v: ton(v) if v > 0 else "-"),
                       "발주 금액(참고)": amt["발주금액"].map(lambda v: won_big(v) if v > 0 else "-")})
    st.dataframe(at, hide_index=True, width="stretch")
    if proj.actionable:
        st.caption(f"발주 금액은 '원료 소요·재고'의 발주 검토량 × 재구매 기준 단가(가정 반영)입니다. 합계 {won_big(float(amt['발주금액'].sum()))}. "
                   "최소 발주량·운임·관세·할인은 반영하지 않습니다.")

    with st.expander("계산 가정"):
        st.write("· 제품 1kg당 원료비 = Σ(배합표 투입량 × 원료 단가). 투입량은 수율을 반영한 값이어야 합니다.")
        st.write("· 장부 기준 단가 = 기준일 재고 + 이후 입고 예정 로트 중 단가가 적힌 로트의 수량 가중 평균. 재구매 기준 = 가장 늦은 입고 예정 로트 단가(없으면 장부 기준).")
        st.write("· 단가가 없는 원료는 0원으로 보지 않고 해당 제품 원료비를 '계산 불가'로 둡니다.")
        st.write("· 부산물 회수 = 투입량 × Σ(부산물률 × 부산물 단가)(부산물.csv, 원료별). 순원료비 = 원료비 − 부산물 회수. 단가 변동 가정은 부산물 단가에 적용하지 않습니다.")
        st.write("· 판매가는 생산계획 행의 판매가_원kg를 계획 물량으로 가중 평균합니다. 원료비율 = 원료비 ÷ 판매가.")
        st.write("· 계획 기간 원료비 = 재고 기준일 이후 명시된 생산계획 물량 × kg당 원료비 (평균 연장 없음).")
        st.write("· 단가 변동 가정은 화면에서만 쓰이며 CSV·발주·알림에 저장되지 않습니다. 시장 소비자가는 참고용 가정입니다.")
