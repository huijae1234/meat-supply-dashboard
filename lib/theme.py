"""디자인 토큰과 스타일. 정보를 읽는 도구라서 색은 최소로 쓴다.

원칙
  - 바탕·글자·선은 중립 회색 톤, 강조색은 파랑 한 가지(차트의 기본 계열도 이 파랑)
  - 빨강·주황·노랑·초록은 '상태'를 뜻할 때만 쓰고, 색만으로 말하지 않는다(점 + 글자 라벨)
  - 카드는 그림자 없이 얇은 테두리, 제목은 굵기를 낮춤, 장식 이모지·채워진 색 상자 없음
  - 차트 계열 색은 검증된 순서(파랑, 주황, 아쿠아, 노랑, 분홍, 초록)로 고정하고 엔티티별로 색을 유지
"""
from __future__ import annotations

# --- 중립 톤
PAGE = "#f9f9f7"      # 페이지 바탕
SURFACE = "#fcfcfb"   # 카드·차트 바탕
SIDEBAR = "#f3f2ee"
INK = "#1a1a19"       # 본문
INK2 = "#52514e"      # 보조 글자 (대비 7:1 이상)
MUTED = "#898781"     # 축 눈금 등 (본문 글자에는 쓰지 않음)
GRID = "#e1e0d9"      # 격자·카드 테두리 (실선, 바탕보다 한 톤)
BASELINE = "#c3c2b7"  # 축선

# --- 강조·계열
ACCENT = "#1c5cab"    # 선택·링크·활성 화면 (파랑 짙은 단계)
BLUE = "#2a78d6"      # 계열 1
ORANGE = "#eb6834"    # 계열 2
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]  # 고정 순서, 순환 금지

# --- 상태 (의미가 있을 때만)
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
TONE = {"정상": "good", "걱정없소": "good", "과잉": "warning", "발주": "serious", "발주필요": "serious", "긴급": "critical", "부족": "critical",
        "ok": "good", "warn": "serious", "bad": "critical"}

# --- 차트 면 채우기 (중립 회색 위주)
BAND = "rgba(11,11,11,0.05)"       # 정상 범위
SHADE = "rgba(11,11,11,0.04)"      # 예측 구간 배경
BAND_BLUE = "rgba(42,120,214,0.12)"
BAND_GRAY = "rgba(82,81,78,0.10)"
BAND_CRIT = "rgba(208,59,59,0.07)"
BAR_SOFT = "rgba(137,135,129,0.35)"


def rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


def country_colors(names) -> dict:
    """같은 나라는 어느 차트에서나 같은 색. '기타'(묶음)는 회색."""
    named = sorted(n for n in names if n != "기타")
    out = {n: SERIES[i % len(SERIES)] for i, n in enumerate(named)}
    out["기타"] = MUTED
    return out


def dot(tone: str) -> str:
    """상태 점 (글자 라벨과 항상 같이 쓴다)."""
    return f'<span class="dot {TONE.get(tone, tone)}"></span>'


def kpi_html(label: str, value: str, sub: str = "", tone: str | None = None, help: str = "") -> str:
    """st.metric과 같은 모양의 카드. 상태 색은 왼쪽 가는 막대 + 값 앞의 점으로만 쓴다."""
    t = TONE.get(tone, tone) if tone else ""
    d = f'<span class="dot {t}"></span>' if t else ""
    s = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    cls = f"kpi {t}" if t else "kpi"
    tip = f' title="{help}"' if help else ""
    return f'<div class="{cls}"{tip}><div class="kpi-label">{label}</div><div class="kpi-value">{d}{value}</div>{s}</div>'


CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&display=swap');
html, body, .stApp, button, input, textarea, select {{font-family:'Pretendard','Noto Sans KR','Malgun Gothic',system-ui,sans-serif !important}}
.stApp{{background:{PAGE};color:{INK}}}
header[data-testid="stHeader"]{{background:transparent}}
[data-testid="stToolbar"], footer{{display:none !important}}
.block-container{{padding-top:1.6rem;padding-bottom:4rem;max-width:1360px}}

/* 헤더 */
.hero{{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:.6rem;padding-bottom:.9rem;margin-bottom:.6rem;border-bottom:1px solid {GRID}}}
.hero h1{{margin:0;padding:0;font-size:1.55rem;font-weight:700;letter-spacing:-.3px;color:{INK};line-height:1.25}}
.hero p{{margin:.3rem 0 0;color:{INK2};font-size:.88rem}}
.meta{{font-size:.8rem;color:{INK2};white-space:nowrap}}
.meta + .meta{{margin-left:1.1rem}}

/* 탭 */
[data-baseweb="tab-list"]{{gap:.1rem;border-bottom:1px solid {GRID}}}
button[data-baseweb="tab"]{{font-size:.9rem;font-weight:500;padding:.6rem .85rem;color:#6f6e69;background:transparent}}
button[data-baseweb="tab"]:nth-child(-n+4){{color:{INK}}}
button[data-baseweb="tab"][aria-selected="true"]{{color:{INK};font-weight:700}}
button[data-baseweb="tab"]:hover{{color:{INK}}}
[data-baseweb="tab-highlight"]{{background:{ACCENT};height:2px}}
[data-baseweb="tab-border"]{{display:none}}

/* 제목 */
h2{{font-size:1.3rem;font-weight:700;letter-spacing:-.2px;color:{INK}}}
h4{{margin-top:1.9rem;margin-bottom:.35rem;font-size:1rem;font-weight:700;color:{INK};letter-spacing:-.1px}}
[data-testid="stCaptionContainer"]{{color:{INK2};font-size:.82rem;line-height:1.6}}

/* 카드: 그림자 없이 얇은 테두리 */
[data-testid="stMetric"], .kpi{{background:{SURFACE};border:1px solid {GRID};border-radius:8px;padding:.8rem 1rem}}
[data-testid="stMetricLabel"], .kpi-label{{color:{INK2};font-size:.8rem;font-weight:500}}
[data-testid="stMetricValue"]{{font-size:1.4rem;font-weight:700;line-height:1.3;color:{INK}}}
[data-testid="stMetricValue"] div,[data-testid="stMetricLabel"] div{{white-space:normal!important;overflow:visible!important;text-overflow:clip!important}}
[data-testid="stMetricDelta"]{{background:transparent!important;padding:0!important;font-size:.8rem}}
.kpi-value{{font-size:1.4rem;font-weight:700;line-height:1.3;color:{INK};margin-top:.1rem}}
.kpi-sub{{font-size:.8rem;color:{INK2};margin-top:.1rem}}
.kpi.good{{border-left:3px solid {STATUS['good']}}}
.kpi.warning{{border-left:3px solid {STATUS['warning']}}}
.kpi.serious{{border-left:3px solid {STATUS['serious']}}}
.kpi.critical{{border-left:3px solid {STATUS['critical']}}}
[data-testid="stPlotlyChart"]{{background:{SURFACE};border:1px solid {GRID};border-radius:8px;padding:.3rem .4rem}}
[data-testid="stDataFrame"]{{border:1px solid {GRID};border-radius:8px;overflow:hidden}}
[data-testid="stVerticalBlockBorderWrapper"]{{border-color:{GRID} !important;border-radius:8px !important;background:{SURFACE}}}
[data-testid="stExpander"]{{background:{SURFACE};border:1px solid {GRID};border-radius:8px}}

/* 안내 상자: 색칠하지 않고 중립 바탕 + 왼쪽 가는 막대 */
[data-testid="stAlert"]{{background:#f3f2ee !important;border:1px solid {GRID} !important;border-left:3px solid {BASELINE} !important;border-radius:8px;color:{INK}}}
[data-testid="stAlert"] *{{color:{INK} !important}}
[data-testid="stAlertContentWarning"]{{}}

/* 상태 점 · 배지 */
.dot{{display:inline-block;width:.55rem;height:.55rem;border-radius:50%;margin-right:.45rem;vertical-align:baseline;background:{MUTED}}}
.dot.good{{background:{STATUS['good']}}}
.dot.warning{{background:{STATUS['warning']}}}
.dot.serious{{background:{STATUS['serious']}}}
.dot.critical{{background:{STATUS['critical']}}}
.vhead{{display:flex;align-items:center;gap:.1rem;font-size:1.25rem;font-weight:700;letter-spacing:-.2px;margin-bottom:.35rem}}
.vhead .sp{{color:{INK2};font-weight:500;margin-right:.5rem}}

/* 왼쪽 내비게이션: 그룹 제목은 작게, 선택한 화면은 연한 바탕 + 왼쪽 파란 막대 */
[data-testid="stSidebarNav"]{{padding-top:.4rem}}
[data-testid="stNavSectionHeader"]{{color:{INK2};font-size:.74rem;font-weight:700;letter-spacing:.02em;margin-top:.5rem}}
[data-testid="stSidebarNavLink"]{{border-radius:6px;font-size:.9rem;color:{INK2};padding:.3rem .6rem;border-left:3px solid transparent}}
[data-testid="stSidebarNavLink"]:hover{{background:#eceae4;color:{INK}}}
[data-testid="stSidebarNavLink"][aria-current="page"]{{background:#e6e4de;color:{INK};font-weight:700;border-left-color:{ACCENT}}}

/* 사이드바·입력 */
[data-testid="stSidebar"]{{background:{SIDEBAR};border-right:1px solid {GRID}}}
[data-testid="stSidebar"] h2{{font-size:1rem;font-weight:700}}
[data-testid="stSidebar"] h3{{font-size:.85rem;font-weight:700;color:{INK2}}}
.stButton button, .stDownloadButton button{{border-radius:6px;border:1px solid {BASELINE};background:{SURFACE};color:{INK};font-weight:500}}
.stButton button:hover, .stDownloadButton button:hover{{border-color:{ACCENT};color:{ACCENT}}}
[data-baseweb="select"] > div, [data-baseweb="input"] > div{{border-radius:6px}}
</style>
"""
