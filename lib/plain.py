"""현장 용어로 바꿔 쓰는 도우미. z-score, σ 같은 통계 용어 대신 '평소 대비 몇 % 적음'처럼 말한다."""
from __future__ import annotations

import math

import pandas as pd

WEEKDAYS = "월화수목금토일"
VERDICT = {"정상": "걱정 없소", "부족": "이럼 안 돼지", "과잉": "넘쳐도 안 돼지"}
VERDICT_ICON = {"정상": "🟢", "부족": "🔴", "과잉": "🟡"}


def ton(kg: float, digits: int = 1) -> str:
    """kg -> '8.2톤'. 100kg 미만은 kg로 쓴다."""
    if kg is None or (isinstance(kg, float) and math.isnan(kg)):
        return "-"
    if abs(kg) < 100:
        return f"{kg:,.0f}kg"
    return f"{kg / 1000:,.{digits}f}톤"


def md(ts) -> str:
    """날짜 -> '10/14(수)'."""
    if ts is None or pd.isna(ts):
        return "-"
    ts = pd.Timestamp(ts)
    return f"{ts.month}/{ts.day}({WEEKDAYS[ts.weekday()]})"


def days(v: float) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "충분"
    return f"{v:.1f}일치"


def vs_normal(pct: float) -> str:
    """기대값 대비 % -> '평소보다 29% 적음'."""
    if abs(pct) < 1:
        return "평소와 비슷함"
    return f"평소보다 {abs(pct):.0f}% {'많음' if pct > 0 else '적음'}"


def vs_year(pct: float) -> str:
    """평년 대비 % -> '평년보다 12% 비쌈'."""
    if pct is None or (isinstance(pct, float) and math.isnan(pct)):
        return "평년 비교 없음"
    if abs(pct) < 1:
        return "평년과 비슷함"
    return f"평년보다 {abs(pct):.0f}% {'비쌈' if pct > 0 else '쌈'}"


def change_text(pct: float, unit: str = "30일") -> str:
    if pct is None or (isinstance(pct, float) and math.isnan(pct)):
        return ""
    if abs(pct) < 0.5:
        return f"{unit} 전과 비슷"
    return f"{unit} 전보다 {abs(pct):.1f}% {'오름' if pct > 0 else '내림'}"


def kg_per_day(kg: float) -> str:
    return f"하루 {ton(kg)}"


def deviation_word(z: float) -> str:
    """부위별 편차 점수 -> '꽤 적음' 같은 말. 화면에서 z 숫자를 직접 보이지 않기 위해 쓴다."""
    if z is None or (isinstance(z, float) and math.isnan(z)):
        return "-"
    size = "매우 " if abs(z) >= 2.5 else "꽤 " if abs(z) >= 1.5 else "조금 " if abs(z) >= 0.5 else ""
    if not size and abs(z) < 0.5:
        return "비슷함"
    return size + ("많음" if z > 0 else "적음")


MODEL_NAMES = {"감쇠 추세(ETS)": "추세 이어가기", "추세+계절(릿지)": "추세+계절 반영", "직전값": "직전 값 그대로"}


def model_name(name: str) -> str:
    return MODEL_NAMES.get(name, name)
