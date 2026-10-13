"""자사(공장) 데이터: CSV 4종을 읽어 검증하고, 원료별 재고 소진·발주 시점을 계산한다.

CSV 4종 (한글 헤더, 단위 kg)
  재고.csv      기준일, 육종, 부위, 원산지, 보관, 재고량_kg, [안전재고일수], [리드타임일수]
  입고예정.csv  입고예정일, 육종, 부위, 원산지, 입고량_kg, [발주일], [공급처]
  생산계획.csv  날짜, 제품, 계획물량_kg
  배합표.csv    제품, 육종, 부위, 원산지, 투입량_kg   (제품 1kg을 만들 때 들어가는 원료 kg)

계산 가정 (화면에도 표시한다)
  - 재고는 '기준일 마감' 값이다. 소요량은 기준일 다음 날부터 센다.
  - 생산계획이 끝난 뒤 날짜는 계획 기간의 하루 평균으로 이어 간다.
  - 원료의 구분 단위는 (육종, 부위, 원산지)이다. 보관(냉장/냉동)은 기본 안전재고·리드타임을 고르는 데 쓴다.
"""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
COMPANY_DIR = ROOT / "data" / "company"  # 올린 파일 보관 위치 (git 제외)

HORIZON_DAYS = 60
DEFAULT_SAFETY = {("국산", "냉장"): 5, ("국산", "냉동"): 10, ("수입", "냉장"): 10, ("수입", "냉동"): 14}
DEFAULT_LEAD = {"국산": 2, "수입": 30}
ROUND_KG = 100  # 발주량은 100kg 단위로 올림
DEFAULT_CYCLE = {"국산": 3, "수입": 14}  # 발주 주기(일). 한 번 발주로 안전재고 + 이 기간 사용량까지 채운다

# 부위 목록: 현장 부위명 -> 시장 데이터 연결 키 (stock = 부위별 재고의 부위명, price = 소비자가 품목명)
PARTS = {
    "돼지": {
        "앞다리": {"stock": "전지", "price": "앞다리"},
        "뒷다리": {"stock": "후지", "price": None},
        "삼겹살": {"stock": "삼겹살", "price": "삼겹살"},
        "목살": {"stock": "목심", "price": "목심"},
        "갈비": {"stock": "갈비", "price": "갈비"},
        "안심": {"stock": "안심", "price": None},
        "등심": {"stock": "등심", "price": None},
        "특수부위": {"stock": "특수부위", "price": None},
        "잡육": {"stock": "잡육", "price": None},
    },
    "소": {
        "양지": {"stock": "양지", "price": "양지"},
        "사태": {"stock": "사태", "price": None},
        "갈비": {"stock": "갈비", "price": "갈비"},
        "안심": {"stock": "안심", "price": "안심"},
        "등심": {"stock": "등심", "price": "등심"},
        "채끝": {"stock": "채끝", "price": None},
        "목심": {"stock": "목심", "price": None},
        "앞다리": {"stock": "앞다리", "price": None},
        "우둔": {"stock": "우둔", "price": None},
        "설도": {"stock": "설도", "price": "설도"},
        "특수부위": {"stock": "특수부위", "price": None},
        "잡육": {"stock": "잡육", "price": None},
    },
}
SPECIES_ALIASES = {"돼지고기": "돼지", "pork": "돼지", "쇠고기": "소", "소고기": "소", "한우": "소", "beef": "소"}
PART_ALIASES = {"돼지": {"전지": "앞다리", "후지": "뒷다리", "목심": "목살"}, "소": {}}
ORIGIN_ALIASES = {"국내산": "국산", "국내": "국산", "domestic": "국산", "수입산": "수입", "import": "수입"}

FILES = {"inventory": "재고.csv", "inbound": "입고예정.csv", "plan": "생산계획.csv", "recipe": "배합표.csv"}
FILE_LABELS = {"inventory": "재고", "inbound": "입고 예정", "plan": "생산계획", "recipe": "배합표"}
REQUIRED = {
    "inventory": ["기준일", "육종", "부위", "원산지", "보관", "재고량_kg"],
    "inbound": ["입고예정일", "육종", "부위", "원산지", "입고량_kg"],
    "plan": ["날짜", "제품", "계획물량_kg"],
    "recipe": ["제품", "육종", "부위", "원산지", "투입량_kg"],
}
OPTIONAL = {"inventory": ["안전재고일수", "리드타임일수", "발주주기일"], "inbound": ["발주일", "공급처"], "plan": [], "recipe": []}
NEEDED_FOR_CALC = ("inventory", "plan", "recipe")  # 입고 예정은 없어도 계산한다


def _norm(name: str) -> str:
    return re.sub(r"[\s_()\-\[\]]", "", str(name)).lower()


def _canonical_columns(kind: str) -> dict[str, str]:
    cols = REQUIRED[kind] + OPTIONAL[kind]
    out = {_norm(c): c for c in cols}
    out.update({_norm(c.replace("_kg", "")): c for c in cols if c.endswith("_kg")})  # '재고량'도 인정
    return out


def read_csv_bytes(raw: bytes) -> pd.DataFrame:
    """엑셀에서 저장한 CSV(cp949)와 UTF-8을 모두 읽는다."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV 인코딩을 읽지 못했습니다 (UTF-8 또는 엑셀 CSV로 저장해 주세요).")


@dataclass
class Issue:
    level: str  # 오류 | 경고
    file: str
    message: str


@dataclass
class CompanyData:
    inventory: pd.DataFrame | None = None
    inbound: pd.DataFrame | None = None
    plan: pd.DataFrame | None = None
    recipe: pd.DataFrame | None = None
    issues: list[Issue] = field(default_factory=list)

    def ready(self) -> bool:
        return all(getattr(self, k) is not None and len(getattr(self, k)) for k in NEEDED_FOR_CALC) and \
            not any(i.level == "오류" for i in self.issues)


def _std_species(v) -> str:
    s = str(v).strip()
    return SPECIES_ALIASES.get(s.lower(), SPECIES_ALIASES.get(s, s))


def _std_part(species: str, v) -> str:
    s = str(v).strip()
    return PART_ALIASES.get(species, {}).get(s, s)


def _std_origin(v) -> str:
    s = str(v).strip()
    return ORIGIN_ALIASES.get(s.lower(), ORIGIN_ALIASES.get(s, s))


def clean_table(kind: str, raw: pd.DataFrame, issues: list[Issue]) -> pd.DataFrame | None:
    """열 이름을 표준화하고 값을 검증한다. 필수 열이 없으면 None."""
    label = FILE_LABELS[kind]
    canon = _canonical_columns(kind)
    df = raw.rename(columns={c: canon.get(_norm(c), c) for c in raw.columns}).copy()
    missing = [c for c in REQUIRED[kind] if c not in df.columns]
    if missing:
        issues.append(Issue("오류", label, "필수 열이 없습니다: " + ", ".join(missing)))
        return None
    df = df.dropna(how="all")
    for c in [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_datetime64_any_dtype(df[c])]:
        df[c] = df[c].astype(str).str.strip()

    if "육종" in df:
        df["육종"] = df["육종"].map(_std_species)
        bad = sorted(set(df["육종"]) - set(PARTS))
        if bad:
            issues.append(Issue("오류", label, f"알 수 없는 육종: {', '.join(bad)} (돼지/소 중에서 입력)"))
            return None
        df["부위"] = [_std_part(s, p) for s, p in zip(df["육종"], df["부위"])]
        unknown = sorted({f"{s} {p}" for s, p in zip(df["육종"], df["부위"]) if p not in PARTS[s]})
        if unknown:
            issues.append(Issue("오류", label, "부위 목록에 없는 이름: " + ", ".join(unknown) + " (양식의 '부위목록' 참고)"))
            return None
    if "원산지" in df:
        df["원산지"] = df["원산지"].map(_std_origin)
        bad = sorted(set(df["원산지"]) - {"국산", "수입"})
        if bad:
            issues.append(Issue("오류", label, f"원산지는 국산/수입 중 하나여야 합니다: {', '.join(bad)}"))
            return None
    if "보관" in df:
        bad = sorted(set(df["보관"]) - {"냉장", "냉동"})
        if bad:
            issues.append(Issue("오류", label, f"보관은 냉장/냉동 중 하나여야 합니다: {', '.join(bad)}"))
            return None

    for c in [c for c in df.columns if c.endswith("_kg")] + [c for c in ("안전재고일수", "리드타임일수", "발주주기일") if c in df.columns]:
        df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", "", regex=False), errors="coerce")
    for c in [c for c in df.columns if c.endswith("_kg")]:
        if df[c].isna().any():
            issues.append(Issue("오류", label, f"'{c}'에 숫자가 아닌 값이 있습니다 ({int(df[c].isna().sum())}행)"))
            return None
        if (df[c] < 0).any():
            issues.append(Issue("오류", label, f"'{c}'에 음수가 있습니다"))
            return None
    for c in [c for c in ("기준일", "입고예정일", "날짜", "발주일") if c in df.columns]:
        parsed = pd.to_datetime(df[c], errors="coerce")
        if c != "발주일" and parsed.isna().any():
            issues.append(Issue("오류", label, f"'{c}'의 날짜 형식이 올바르지 않습니다 (예: 2026-10-14)"))
            return None
        df[c] = parsed.dt.normalize()
    return df.reset_index(drop=True)


def load_data(raw_tables: dict[str, pd.DataFrame | None]) -> CompanyData:
    data = CompanyData()
    for kind in FILES:
        raw = raw_tables.get(kind)
        if raw is None:
            if kind in NEEDED_FOR_CALC:
                data.issues.append(Issue("오류", FILE_LABELS[kind], "파일이 없습니다"))
            continue
        setattr(data, kind, clean_table(kind, raw, data.issues))
    _cross_check(data)
    return data


def _cross_check(d: CompanyData) -> None:
    if d.plan is None or d.recipe is None:
        return
    no_recipe = sorted(set(d.plan["제품"]) - set(d.recipe["제품"]))
    if no_recipe:
        d.issues.append(Issue("경고", "생산계획", "배합표에 없는 제품은 원료 소요량에서 빠집니다: " + ", ".join(no_recipe)))
    unused = sorted(set(d.recipe["제품"]) - set(d.plan["제품"]))
    if unused:
        d.issues.append(Issue("참고", "배합표", "생산계획에 없는 제품(소요량 0): " + ", ".join(unused)))
    if d.inventory is not None:
        inv_keys = set(zip(d.inventory["육종"], d.inventory["부위"], d.inventory["원산지"]))
        need = set(zip(d.recipe["육종"], d.recipe["부위"], d.recipe["원산지"]))
        no_stock = sorted(f"{s} {p}({o})" for s, p, o in need - inv_keys)
        if no_stock:
            d.issues.append(Issue("경고", "재고", "배합표에 쓰이지만 재고가 없는 원료(재고 0으로 계산): " + ", ".join(no_stock)))
        dup = d.inventory.duplicated(["기준일", "육종", "부위", "원산지", "보관"], keep=False)
        if dup.any():
            d.issues.append(Issue("경고", "재고", "같은 날짜·원료·보관의 중복 행이 있어 합산합니다"))
    if d.plan is not None and d.plan["날짜"].min() > (d.inventory["기준일"].max() if d.inventory is not None else d.plan["날짜"].min()) + timedelta(days=3):
        d.issues.append(Issue("경고", "생산계획", "계획이 재고 기준일보다 3일 넘게 늦게 시작합니다"))


# ------------------------------------------------------------------ 계산
@dataclass
class Projection:
    base_date: pd.Timestamp
    summary: pd.DataFrame      # 원료별 한 줄
    daily: pd.DataFrame        # 원료 x 날짜 예상 재고
    notes: list[str]


@dataclass
class Settings:
    """CSV에 값이 없을 때 쓰는 기본값. 화면에서 바꿀 수 있다."""
    safety: dict = field(default_factory=lambda: dict(DEFAULT_SAFETY))
    lead: dict = field(default_factory=lambda: dict(DEFAULT_LEAD))
    cycle: dict = field(default_factory=lambda: dict(DEFAULT_CYCLE))


def _share_text(d: dict) -> str:
    tot = sum(d.values())
    if tot <= 0:
        return ""
    return " · ".join(f"{p} {v / tot * 100:.0f}%" for p, v in sorted(d.items(), key=lambda x: -x[1]))


def ingredient_label(species: str, part: str, origin: str) -> str:
    return f"{species} {part}({origin})"


def _plan_daily(plan: pd.DataFrame, base: pd.Timestamp, dates: pd.DatetimeIndex, notes: list[str]) -> pd.DataFrame:
    """제품 x 날짜 계획. 계획 뒤는 계획 기간의 일평균으로 이어 간다."""
    p = plan[plan["날짜"] > base].groupby(["날짜", "제품"])["계획물량_kg"].sum().unstack(fill_value=0.0)
    if p.empty:
        p_all = plan.groupby("제품")["계획물량_kg"].sum() / max(plan["날짜"].nunique(), 1)
        notes.append("생산계획이 모두 기준일 이전이라 전체 기간의 하루 평균으로 가정했습니다")
        return pd.DataFrame({prod: [v] * len(dates) for prod, v in p_all.items()}, index=dates)
    last = p.index.max()
    span = pd.date_range(base + timedelta(days=1), last)
    p = p.reindex(span, fill_value=0.0)
    avg = p.mean()
    out = p.reindex(dates)
    beyond = dates > last
    for prod in out.columns:
        out.loc[beyond, prod] = avg[prod]
    if beyond.any():
        notes.append(f"{last:%m/%d} 이후는 생산계획 기간의 하루 평균으로 이어 갔습니다")
    return out.fillna(0.0)


def project(data: CompanyData, horizon: int = HORIZON_DAYS, today: pd.Timestamp | None = None,
            settings: Settings | None = None) -> Projection:
    st_ = settings or Settings()
    inv, inb, plan, rec = data.inventory, data.inbound, data.plan, data.recipe
    notes: list[str] = []
    base = inv["기준일"].max()
    ref = base if today is None else max(pd.Timestamp(today).normalize(), base)
    dates = pd.date_range(base + timedelta(days=1), periods=horizon)

    pd_daily = _plan_daily(plan, base, dates, notes)
    key = ["육종", "부위", "원산지"]
    r = rec.groupby(["제품"] + key)["투입량_kg"].sum().reset_index()
    demand, prod_dem = {}, {}
    for _, row in r.iterrows():
        if row["제품"] not in pd_daily.columns:
            continue
        k = (row["육종"], row["부위"], row["원산지"])
        demand.setdefault(k, pd.Series(0.0, index=dates))
        demand[k] = demand[k] + pd_daily[row["제품"]] * row["투입량_kg"]
        prod_dem.setdefault(k, {})[row["제품"]] = prod_dem.get(k, {}).get(row["제품"], 0.0) + float((pd_daily[row["제품"]].iloc[:14] * row["투입량_kg"]).sum())

    keys = set(demand) | set(zip(inv["육종"], inv["부위"], inv["원산지"]))
    if inb is not None and len(inb):
        keys |= set(zip(inb["육종"], inb["부위"], inb["원산지"]))

    inbound_by = {}
    if inb is not None and len(inb):
        past = inb[inb["입고예정일"] <= base]
        if len(past):
            notes.append(f"기준일({base:%m/%d}) 이전 입고 예정 {len(past)}건은 이미 재고에 반영된 것으로 보고 제외했습니다")
        fut = inb[inb["입고예정일"] > base]
        for k, g in fut.groupby(key):
            inbound_by[k] = g.groupby("입고예정일")["입고량_kg"].sum().reindex(dates, fill_value=0.0)

    rows, daily_parts = [], []
    for k in sorted(keys):
        species, part, origin = k
        g_inv = inv[(inv["육종"] == species) & (inv["부위"] == part) & (inv["원산지"] == origin)]
        if len(g_inv):
            last_day = g_inv["기준일"].max()
            cur = g_inv[g_inv["기준일"] == last_day]
            s0 = float(cur["재고량_kg"].sum())
            storage = cur["보관"].iloc[0]
            if last_day < base:
                notes.append(f"{ingredient_label(*k)} 재고 기준일이 {(base - last_day).days}일 전({last_day:%m/%d})입니다")
        else:
            s0, storage = 0.0, ("냉동" if origin == "수입" else "냉장")
        def _col(name, default):
            return float(g_inv[name].max()) if len(g_inv) and name in g_inv and g_inv[name].notna().any() else float(default)
        safety_days = _col("안전재고일수", st_.safety[(origin, storage)])
        lead_days = _col("리드타임일수", st_.lead[origin])
        cycle_days = _col("발주주기일", st_.cycle[origin])
        dem = demand.get(k, pd.Series(0.0, index=dates))
        inc = inbound_by.get(k, pd.Series(0.0, index=dates))
        stock = s0 + (inc - dem).cumsum()
        avg7 = float(dem.iloc[:7].mean())
        safety_qty = safety_days * avg7

        cum_dem = dem.cumsum()
        run_no_inbound = cum_dem[cum_dem > s0]
        if avg7 <= 0:
            cover_days = math.inf
        elif len(run_no_inbound):
            i = int((cum_dem > s0).values.argmax())
            prev = float(cum_dem.iloc[i - 1]) if i > 0 else 0.0
            cover_days = i + (s0 - prev) / max(float(dem.iloc[i]), 1e-9)
        else:
            cover_days = math.inf
        neg = stock[stock < 0]
        runout = neg.index[0] if len(neg) else None
        below = stock[stock < safety_qty - 1e-9] if safety_qty > 0 else stock[stock < 0]
        breach = below.index[0] if len(below) else None
        arrival = ref + timedelta(days=int(lead_days))

        status, order_qty, order_by, gap = "정상", 0.0, None, 0.0
        under_days, under_end = 0, None
        if breach is not None:
            eff = max(breach, arrival)
            at_arrival = float(stock.loc[eff]) if eff in stock.index else float(stock.iloc[-1])
            target = safety_qty + cycle_days * avg7  # 도착 시점에 맞춰 둘 목표 재고
            order_qty = max(target - at_arrival, 0.0)
            order_qty = math.ceil(order_qty / ROUND_KG) * ROUND_KG if order_qty > 0 else 0.0
            order_by = max(eff - timedelta(days=int(lead_days)), ref)
            pre = stock[stock.index < arrival]
            gap = float(max(-pre.min(), 0.0)) if len(pre) else 0.0
            low = pre[pre < safety_qty - 1e-9]
            under_days = int(len(low))                        # 입고 전 안전재고 아래로 지내는 실제 일수 (이미 잡힌 입고로 회복되면 그만큼 뺀다)
            under_end = low.index[-1] if len(low) else None
            if runout is not None and runout < arrival:
                status = "긴급"
            elif order_qty > 0 and (order_by - ref).days <= cycle_days:
                status = "발주필요"  # 이번 발주 주기 안에 발주해야 하는 것만 '할 일'로 올린다 (더 먼 것은 발주 기한만 보여준다)
        if status == "정상" and avg7 > 0:
            over_days = max(safety_days * 4, 20)
            if (s0 + float(inc.sum())) / avg7 > over_days:
                status = "과잉"

        rows.append({
            "육종": species, "부위": part, "원산지": origin, "보관": storage, "원료": ingredient_label(*k),
            "현재고_kg": s0, "일평균사용_kg": avg7, "재고일수": cover_days, "안전재고일수": safety_days,
            "리드타임일수": lead_days, "소진예정일": runout, "안전재고이하일": breach, "상태": status,
            "발주기한": order_by, "발주량_kg": order_qty, "공백량_kg": gap, "가장빠른입고일": arrival, "안전재고미달일수": under_days, "안전재고미달종료일": under_end, "발주주기일": cycle_days, "영향제품": _share_text(prod_dem.get(k, {})), "입고예정_kg": float(inc.sum()),
        })
        daily_parts.append(pd.DataFrame({"원료": ingredient_label(*k), "날짜": dates, "예상재고_kg": stock.values,
                                          "소요량_kg": dem.values, "입고_kg": inc.values, "안전재고_kg": safety_qty}))

    summary = pd.DataFrame(rows)
    order = {"긴급": 0, "발주필요": 1, "과잉": 2, "정상": 3}
    if len(summary):
        summary = summary.sort_values(["상태", "재고일수"], key=lambda s: s.map(order) if s.name == "상태" else s).reset_index(drop=True)
    daily = pd.concat(daily_parts, ignore_index=True) if daily_parts else pd.DataFrame()
    return Projection(base_date=base, summary=summary, daily=daily, notes=notes)


# ------------------------------------------------------------------ 저장 / 불러오기 / 양식
def save_raw(kind: str, raw: bytes) -> None:
    COMPANY_DIR.mkdir(parents=True, exist_ok=True)
    (COMPANY_DIR / FILES[kind]).write_bytes(raw)


def load_saved() -> dict[str, pd.DataFrame | None]:
    out = {}
    for kind, name in FILES.items():
        p = COMPANY_DIR / name
        out[kind] = read_csv_bytes(p.read_bytes()) if p.exists() else None
    return out


def clear_saved() -> int:
    n = 0
    for name in FILES.values():
        p = COMPANY_DIR / name
        if p.exists():
            p.unlink()
            n += 1
    return n


def sample_tables(today: date | None = None) -> dict[str, pd.DataFrame]:
    """설명용 가짜 데이터. 날짜는 '오늘' 기준으로 만든다 (실제 회사 데이터가 아님)."""
    t = pd.Timestamp(today or date.today()).normalize()
    d = lambda n: (t + timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    inventory = pd.DataFrame([
        [d(0), "돼지", "앞다리", "국산", "냉장", 9000, None, None],
        [d(0), "돼지", "삼겹살", "국산", "냉장", 5200, None, None],
        [d(0), "돼지", "뒷다리", "수입", "냉동", 42000, None, None],
        [d(0), "소", "양지", "수입", "냉동", 2000, None, None],
        [d(0), "돼지", "갈비", "국산", "냉장", 15000, None, None],
        [d(0), "소", "사태", "국산", "냉장", 2300, 4, 2],
    ], columns=["기준일", "육종", "부위", "원산지", "보관", "재고량_kg", "안전재고일수", "리드타임일수"])
    inbound = pd.DataFrame([
        [d(6), "돼지", "앞다리", "국산", 6000, d(4), "A축산"],
        [d(35), "소", "양지", "수입", 8000, d(-5), "B무역"],
    ], columns=["입고예정일", "육종", "부위", "원산지", "입고량_kg", "발주일", "공급처"])
    days = [d(i) for i in range(1, 15) if (t + timedelta(days=i)).weekday() < 5]
    plan = pd.DataFrame([[x, p, q] for x in days for p, q in (("프레스햄", 3000), ("소시지", 2500), ("베이컨", 1200), ("육개장", 900), ("양념갈비", 400))],
                        columns=["날짜", "제품", "계획물량_kg"])
    recipe = pd.DataFrame([
        ["프레스햄", "돼지", "앞다리", "국산", 0.55], ["프레스햄", "돼지", "뒷다리", "수입", 0.30],
        ["소시지", "돼지", "뒷다리", "수입", 0.45], ["소시지", "돼지", "앞다리", "국산", 0.20],
        ["베이컨", "돼지", "삼겹살", "국산", 1.05],
        ["육개장", "소", "양지", "수입", 0.30], ["육개장", "소", "사태", "국산", 0.10],
        ["양념갈비", "돼지", "갈비", "국산", 0.90],
    ], columns=["제품", "육종", "부위", "원산지", "투입량_kg"])
    return {"inventory": inventory, "inbound": inbound, "plan": plan, "recipe": recipe}


def part_list_text() -> str:
    lines = ["# 부위목록 (육종별로 이 이름 중에서 입력)"]
    for sp, parts in PARTS.items():
        lines.append(f"{sp}: " + ", ".join(parts))
    return "\n".join(lines)
