"""디자인 토큰과 스타일. 정보를 읽는 도구라서 장식은 아끼고, 색은 '구조'와 '상태'를 알려 주는 데 쓴다.

원칙
  - 메인 색 두 가지(짙은 네이비 → 딥 청록)를 그라데이션으로 포인트에만 쓴다: 상단 헤더 띠, 사이드바, 화면 제목, 소제목 막대,
    카드 위쪽 띠, 버튼 hover, 추이 차트 실적선 아래. 표·글자 영역은 흰 바탕으로 깔끔하게 둔다.
  - 빨강·주황·노랑·초록은 '상태'를 뜻할 때만 쓰고, 색만으로 말하지 않는다(점 + 글자 라벨). 메인 색과 겹치지 않게 네이비·청록을 골랐다.
  - 차트 계열 색은 고정 순서로 엔티티별 색을 유지한다(첫 계열만 메인 파랑)
  - Streamlit 기본 색(위젯·사이드바 글자)은 .streamlit/config.toml에서 같은 값으로 맞춘다
"""
from __future__ import annotations

# --- 메인 색 (네이비 + 청록)
PRIMARY = "#19324f"    # 짙은 네이비: 그라데이션 시작
PRIMARY2 = "#0b8783"   # 딥 청록: 그라데이션 끝
ACCENT = "#1f54d4"     # 선택·링크·위젯 강조 (config.toml primaryColor)
TINT = "#e7eef8"       # 정보 상자·hover 바탕
HILITE = "#4fd6c2"     # 사이드바에서 선택한 화면의 막대
SIDEBAR_FROM = "#162b47"
SIDEBAR_TO = "#0b3e4a"
SIDEBAR = SIDEBAR_FROM

# --- 중립 톤
PAGE = "#f1f4f8"      # 페이지 바탕
SURFACE = "#ffffff"   # 카드·차트 바탕
INK = "#18212e"       # 본문
INK2 = "#475365"      # 보조 글자 (대비 7:1 이상)
MUTED = "#808da0"     # 축 눈금 등 (본문 글자에는 쓰지 않음)
GRID = "#dde3eb"      # 격자·카드 테두리
BASELINE = "#c1c9d5"  # 축선·버튼 테두리

# --- 차트 계열 (고정 순서, 순환 금지)
SERIES = ["#2158d4", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
BLUE = SERIES[0]      # 계열 1: 실적·주요 값
ORANGE = SERIES[1]    # 계열 2: 파일 예측 등

# --- 상태 (의미가 있을 때만)
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
TONE = {"정상": "good", "걱정없소": "good", "과잉": "warning", "발주": "serious", "발주필요": "serious", "긴급": "critical", "부족": "critical",
        "ok": "good", "warn": "serious", "bad": "critical"}

# --- 차트 면 채우기 (중립 회색 위주)
BAND = "rgba(11,11,11,0.05)"       # 정상 범위
SHADE = "rgba(11,11,11,0.04)"      # 예측 구간 배경
BAND_GRAY = "rgba(82,81,78,0.10)"
BAND_CRIT = "rgba(208,59,59,0.07)"
BAR_SOFT = "rgba(137,135,129,0.35)"


def rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


BAND_BLUE = rgba(BLUE, 0.12)


def _mix(hex_color: str, t: float, with_hex: str = "#ffffff") -> str:
    """hex_color를 with_hex 쪽으로 t(0~1)만큼 섞은 색."""
    a, b = hex_color.lstrip("#"), with_hex.lstrip("#")
    c = [round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t) for i in (0, 2, 4)]
    return "#" + "".join(f"{v:02x}" for v in c)


def country_colors(names) -> dict:
    """같은 나라는 어느 차트에서나 같은 색. '기타'(묶음)는 회색."""
    named = sorted(n for n in names if n != "기타")
    out = {n: SERIES[i % len(SERIES)] for i, n in enumerate(named)}
    out["기타"] = MUTED
    return out


def soft_area(fig, x, y, name: str, color: str | None = None, width: float = 2, **line) -> None:
    """추이선 + 선 아래 옅은 세로 그라데이션. 축이 0까지 늘어나지 않도록 보이지 않는 바닥선(최솟값 조금 아래)까지만 채운다."""
    import pandas as pd
    import plotly.graph_objects as go
    color = color or BLUE
    ys = pd.Series(list(y), dtype="float64")
    if ys.notna().sum() < 2:
        fig.add_trace(go.Scatter(x=x, y=y, name=name, line=dict(color=color, width=width, **line)))
        return
    lo, hi = float(ys.min()), float(ys.max())
    floor = lo - (hi - lo) * 0.06 if hi > lo else lo * 0.98
    fig.add_trace(go.Scatter(x=x, y=[floor] * len(ys), line=dict(width=0), showlegend=False, hoverinfo="skip", name=""))
    fig.add_trace(go.Scatter(x=x, y=y, name=name, line=dict(color=color, width=width, **line), fill="tonexty",
                             fillgradient=dict(type="vertical", colorscale=[[0, rgba(color, 0.0)], [1, rgba(color, 0.20)]])))


def dot(tone: str) -> str:
    """상태 점 (글자 라벨과 항상 같이 쓴다)."""
    return f'<span class="dot {TONE.get(tone, tone)}"></span>'


def kpi_html(label: str, value: str, sub: str = "", tone: str | None = None, help: str = "") -> str:
    """st.metric과 같은 모양의 카드. 상태 색은 카드 위쪽 띠 + 값 앞의 점으로 쓴다."""
    t = TONE.get(tone, tone) if tone else ""
    d = f'<span class="dot {t}"></span>' if t else ""
    s = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    cls = f"kpi {t}" if t else "kpi"
    tip = f' title="{help}"' if help else ""
    return f'<div class="{cls}"{tip}><div class="kpi-label">{label}</div><div class="kpi-value">{d}{value}</div>{s}</div>'


G = f"linear-gradient(120deg,{PRIMARY} 0%,{PRIMARY2} 100%)"     # 메인 그라데이션
GV = f"linear-gradient(180deg,{PRIMARY2} 0%,{PRIMARY} 100%)"    # 세로(작은 막대)
_S = STATUS
_STRIP = {k: f"linear-gradient(90deg,{v} 0%,{_mix(v, .45)} 100%)" for k, v in _S.items()}
_TINT = {k: _mix(v, .9) for k, v in _S.items()}
_SHADOW = "0 1px 2px rgba(16,24,40,.05),0 4px 12px rgba(16,24,40,.04)"

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;800&display=swap');
html, body, .stApp, button, input, textarea, select {{font-family:'Pretendard','Noto Sans KR','Malgun Gothic',system-ui,sans-serif !important}}
.stApp{{background:{PAGE};color:{INK}}}
::selection{{background:{TINT}}}
header[data-testid="stHeader"]{{background:transparent}}
[data-testid="stToolbar"], footer{{display:none !important}}
.block-container{{padding-top:1.4rem;padding-bottom:4rem;max-width:1360px}}

/* 상단 헤더 띠: 메인 그라데이션 */
.hero{{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:.6rem;padding:1.1rem 1.4rem;margin-bottom:.9rem;
  border-radius:14px;background:{G};box-shadow:0 8px 24px {rgba(PRIMARY, .22)};color:#fff}}
.hero h1{{margin:0;padding:0;font-size:1.55rem;font-weight:800;letter-spacing:-.3px;color:#fff;line-height:1.25}}
.hero p{{margin:.3rem 0 0;color:rgba(255,255,255,.82);font-size:.88rem}}
.meta{{display:inline-flex;align-items:center;font-size:.76rem;color:#fff;white-space:nowrap;background:rgba(255,255,255,.14);
  border:1px solid rgba(255,255,255,.22);padding:.2rem .65rem;border-radius:999px}}
.meta + .meta{{margin-left:.45rem}}
.hero .dot{{box-shadow:0 0 0 2px rgba(255,255,255,.35)}}

/* 화면 제목: 그라데이션 글자, 소제목: 왼쪽 작은 그라데이션 막대 */
[data-testid="stMainBlockContainer"] h3{{background:{G};-webkit-background-clip:text;background-clip:text;color:transparent !important;
  font-weight:800;letter-spacing:-.4px;width:fit-content}}
h2{{font-size:1.3rem;font-weight:700;letter-spacing:-.2px;color:{INK}}}
[data-testid="stMainBlockContainer"] h4{{display:flex;align-items:center;gap:.5rem;margin-top:1.9rem;margin-bottom:.35rem;font-size:1rem;font-weight:700;color:{INK};letter-spacing:-.1px}}
[data-testid="stMainBlockContainer"] h4::before{{content:"";flex:none;width:4px;height:1.05em;border-radius:3px;background:{GV}}}
[data-testid="stCaptionContainer"]{{color:{INK2};font-size:.82rem;line-height:1.6}}

/* 탭 */
[data-baseweb="tab-list"]{{gap:.1rem;border-bottom:1px solid {GRID}}}
button[data-baseweb="tab"]{{font-size:.9rem;font-weight:500;padding:.6rem .85rem;color:{INK2};background:transparent}}
button[data-baseweb="tab"][aria-selected="true"]{{color:{INK};font-weight:700}}
[data-baseweb="tab-highlight"]{{background:{G};height:3px;border-radius:3px}}
[data-baseweb="tab-border"]{{display:none}}

/* 카드: 흰 바탕 + 위쪽 얇은 띠(중립은 메인 그라데이션, 상태는 상태색) */
[data-testid="stMetric"], .kpi{{background:{G} top/100% 3px no-repeat, {SURFACE};border:1px solid {GRID};border-radius:12px;
  padding:.85rem 1rem .8rem;box-shadow:{_SHADOW}}}
[data-testid="stMetricLabel"], .kpi-label{{color:{INK2};font-size:.8rem;font-weight:500}}
[data-testid="stMetricValue"]{{font-size:clamp(1rem,1.45vw,1.4rem);font-weight:800;line-height:1.3;color:{INK}}}
[data-testid="stMetricValue"] div,[data-testid="stMetricLabel"] div{{white-space:normal!important;overflow:visible!important;text-overflow:clip!important}}
[data-testid="stMetricLabel"] p{{white-space:normal !important;word-break:keep-all;line-height:1.35}}
[data-testid="stMetricValue"] p{{white-space:nowrap !important}}
[data-testid="stMetricDelta"]{{background:transparent!important;padding:0!important;font-size:.8rem}}
.kpi-value{{font-size:clamp(1.05rem,1.5vw,1.4rem);font-weight:800;line-height:1.3;color:{INK};margin-top:.1rem}}
.kpi-sub{{font-size:.8rem;color:{INK2};margin-top:.1rem}}
.kpi.good{{background:{_STRIP['good']} top/100% 3px no-repeat, {SURFACE}}}
.kpi.warning{{background:{_STRIP['warning']} top/100% 3px no-repeat, {SURFACE}}}
.kpi.serious{{background:{_STRIP['serious']} top/100% 3px no-repeat, {SURFACE}}}
.kpi.critical{{background:{_STRIP['critical']} top/100% 3px no-repeat, {SURFACE}}}
[data-testid="stPlotlyChart"]{{background:{SURFACE};border:1px solid {GRID};border-radius:12px;padding:.35rem .45rem;box-shadow:{_SHADOW}}}
[data-testid="stDataFrame"]{{border:1px solid {GRID};border-radius:10px;overflow:hidden;box-shadow:{_SHADOW}}}
[data-testid="stVerticalBlockBorderWrapper"]{{border-color:{GRID} !important;border-radius:12px !important;background:{SURFACE};box-shadow:{_SHADOW}}}
[data-testid="stExpander"] details{{background:{SURFACE};border:1px solid {GRID};border-radius:12px;box-shadow:{_SHADOW}}}
[data-testid="stExpander"] summary:hover{{background:{TINT};border-radius:12px}}
[data-testid="stExpander"] summary:hover *{{color:{INK} !important}}

/* 안내 상자: 연한 색 바탕 + 왼쪽 막대(정보=메인 색, 나머지=상태색) */
[data-testid="stAlert"]{{background:transparent !important;border:none !important}}
[data-testid="stAlertContainer"]{{background:{TINT} !important;border:1px solid {_mix(ACCENT, .8)};border-left:4px solid {PRIMARY};border-radius:10px;color:{INK}}}
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]){{background:{_TINT['good']} !important;border-color:{_mix(_S['good'], .75)};border-left-color:{_S['good']}}}
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]){{background:{_TINT['warning']} !important;border-color:{_mix(_S['warning'], .65)};border-left-color:{_S['warning']}}}
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]){{background:{_TINT['critical']} !important;border-color:{_mix(_S['critical'], .75)};border-left-color:{_S['critical']}}}
[data-testid="stAlertContainer"] *{{color:{INK} !important}}

/* 상태 점 · 배지 */
.dot{{display:inline-block;width:.55rem;height:.55rem;border-radius:50%;margin-right:.45rem;vertical-align:baseline;background:{MUTED}}}
.dot.good{{background:{_S['good']}}}
.dot.warning{{background:{_S['warning']}}}
.dot.serious{{background:{_S['serious']}}}
.dot.critical{{background:{_S['critical']}}}
.vhead{{display:flex;align-items:center;gap:.1rem;font-size:1.25rem;font-weight:700;letter-spacing:-.2px;margin-bottom:.35rem}}
.vhead .sp{{color:{INK2};font-weight:500;margin-right:.5rem}}

/* 왼쪽 메뉴: 진한 세로 그라데이션 + 흰 글자, 선택한 화면은 밝은 막대 */
[data-testid="stSidebar"]{{background:linear-gradient(180deg,{SIDEBAR_FROM} 0%,{SIDEBAR_TO} 100%);border-right:none}}
[data-testid="stSidebarNav"]{{padding-top:.4rem}}
[data-testid="stNavSectionHeader"], [data-testid="stNavSectionHeader"] *{{color:rgba(255,255,255,.55) !important;font-size:.72rem;font-weight:700;letter-spacing:.06em}}
[data-testid="stNavSectionHeader"]{{margin-top:.6rem}}
[data-testid="stSidebarNavLink"]{{border-radius:8px;padding:.32rem .65rem;border-left:3px solid transparent;transition:background .15s ease}}
[data-testid="stSidebarNavLink"] span, [data-testid="stSidebarNavLink"] p{{color:rgba(255,255,255,.80) !important;font-size:.9rem}}
[data-testid="stSidebarNavLink"]:hover{{background:rgba(255,255,255,.08)}}
[data-testid="stSidebarNavLink"]:hover span, [data-testid="stSidebarNavLink"]:hover p{{color:#fff !important}}
[data-testid="stSidebarNavLink"][aria-current="page"]{{background:linear-gradient(90deg,rgba(255,255,255,.18) 0%,rgba(255,255,255,.05) 100%);border-left-color:{HILITE}}}
[data-testid="stSidebarNavLink"][aria-current="page"] span, [data-testid="stSidebarNavLink"][aria-current="page"] p{{color:#fff !important;font-weight:700}}
[data-testid="stSidebarHeader"] *, [data-testid="stSidebarCollapseButton"] *{{color:rgba(255,255,255,.75) !important}}
[data-testid="stSidebar"] h2{{font-size:1rem;font-weight:700;color:#fff}}
[data-testid="stSidebar"] h3{{font-size:.85rem;font-weight:700;color:rgba(255,255,255,.75)}}

/* 버튼: 평소엔 흰 바탕, 올리면 메인 그라데이션 */
.stButton button, .stDownloadButton button, [data-testid="stPopoverButton"]{{border-radius:8px;border:1px solid {BASELINE};background:{SURFACE};color:{INK};
  font-weight:500;transition:background .15s ease, color .15s ease, border-color .15s ease}}
.stButton button:hover, .stDownloadButton button:hover, [data-testid="stPopoverButton"]:hover{{border-color:transparent;background:{G};color:#fff}}
.stButton button:hover *, .stDownloadButton button:hover *, [data-testid="stPopoverButton"]:hover *{{color:#fff !important}}
.stButton button:disabled, .stDownloadButton button:disabled, .stButton button:disabled:hover, .stDownloadButton button:disabled:hover{{background:{SURFACE};color:{MUTED};border-color:{GRID}}}
.stButton button:disabled:hover *, .stDownloadButton button:disabled:hover *{{color:{MUTED} !important}}
.stButton button[kind="primary"]{{border:none;background:{G};color:#fff}}
[data-baseweb="select"] > div, [data-baseweb="input"] > div{{border-radius:8px}}
[data-testid="stPopoverBody"]{{border-radius:12px}}
</style>
"""
