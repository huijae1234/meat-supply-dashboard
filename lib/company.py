"""자사(공장) 데이터: CSV 4종(+ 선택 대체표)을 읽어 검증하고, 원료별 재고 소진·발주 시점을 계산한다.

CSV 4종 (한글 헤더, 단위 kg)
  재고.csv      기준일, 육종, 부위, 원산지, 보관, 재고량_kg, [안전재고일수], [리드타임일수]
  입고예정.csv  입고예정일, 육종, 부위, 원산지, 입고량_kg, [발주일], [공급처]
  생산계획.csv  날짜, 제품, 계획물량_kg
  배합표.csv    제품, 육종, 부위, 원산지, 투입량_kg   (제품 1kg을 만들 때 들어가는 원료 kg)

계산 가정 (화면에도 표시한다)
  - 재고는 '기준일 마감' 값이다. 소요량은 기준일 다음 날부터 센다.
  - 생산계획이 끝난 뒤 날짜는 계획 기간의 하루 평균으로 이어 간다.
  - 원료는 (육종, 부위, 원산지, 보관)으로 구분하며 로트의 사용 가능 조건을 확인한다.
"""
from __future__ import annotations

import io
import json
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
DEFAULT_THAW_DAYS = {"국산": 1, "수입": 2}      # 냉동 원료: 해동 시작 -> 생산 투입까지 일수 (설명용 초기값)
DEFAULT_THAW_LOSS = {"국산": 2.0, "수입": 3.0}  # 냉동 원료: 해동 감량률(%). 배합표 투입량은 해동 후 무게 기준
DEFAULT_THAW_WEEKDAYS = [0, 1, 2, 3, 4]         # 해동실에 넣을 수 있는 요일 (월=0 … 일=6)
DEFAULT_THAW_HOLD = 2                            # 해동이 끝난 뒤 쓰기 전까지 냉장 대기 최대 일수 (설명용 초기값)
DEFAULT_SPOT_PREMIUM = 15.0                      # 긴급 현물 구매 단가 할증(%) (설명용 초기값)
DEFAULT_SPOT_LEAD = {"국산": 1, "수입": 3}        # 긴급 현물이 들어오기까지 일수 (수입은 국내 재고 보유처 기준)
DEFAULT_EXPIRY_WARN = {"냉장": 3, "냉동": 30}      # 사용기한까지 이 일수 이내면 '임박'
DEFAULT_SHELF = {"냉장": 10, "냉동": 365}          # 사용기한을 안 적은 로트의 가정 기한(들어온 날부터, 재고는 기준일부터) — 경고에만 쓴다

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

FILES = {"inventory": "재고.csv", "inbound": "입고예정.csv", "plan": "생산계획.csv", "recipe": "배합표.csv",
         "substitute": "대체표.csv", "supplier": "공급처.csv"}
FILE_LABELS = {"inventory": "재고", "inbound": "입고 예정", "plan": "생산계획", "recipe": "배합표", "substitute": "대체표", "supplier": "공급처"}
OPTIONAL_FILES = ("inbound", "substitute", "supplier")  # 없어도 계산한다
REQUIRED = {
    "inventory": ["기준일", "육종", "부위", "원산지", "보관", "재고량_kg"],
    "inbound": ["입고예정일", "육종", "부위", "원산지", "입고량_kg"],
    "plan": ["날짜", "제품", "계획물량_kg"],
    "recipe": ["제품", "육종", "부위", "원산지", "투입량_kg"],
    "substitute": ["제품", "육종", "부위", "원산지", "보관", "대체부위", "대체원산지", "대체보관"],
    "supplier": ["육종", "부위", "원산지", "보관", "공급처"],
}
OPTIONAL = {
    "inventory": ["안전재고일수", "리드타임일수", "발주주기일", "로트", "사용상태", "사용가능일", "사용기한", "단가_원kg", "해동일수", "해동감량률"],
    "inbound": ["발주일", "공급처", "보관", "로트", "사용상태", "사용가능일", "사용기한", "단가_원kg", "해동일수", "해동감량률"],
    "plan": ["주문번호", "납기", "우선순위", "판매가_원kg", "위약금_원kg"], "recipe": ["보관"],
    "substitute": ["대체육종", "최대대체비율", "환산계수", "비고"],
    "supplier": ["담당자", "연락처", "최소발주량_kg", "발주단위_kg", "우선순위"],
}
MONEY_COLUMNS = ("단가_원kg", "판매가_원kg", "위약금_원kg")  # 원/kg. 비어 있어도 되지만, 적었으면 0보다 큰 숫자여야 한다
COLUMN_ALIASES = {"단가": "단가_원kg", "매입단가": "단가_원kg", "단가원": "단가_원kg", "단가원/kg": "단가_원kg",
                  "판매가": "판매가_원kg", "납품가": "판매가_원kg", "판매가원": "판매가_원kg", "판매가원/kg": "판매가_원kg"}
MATERIAL_KEY = ["육종", "부위", "원산지", "보관"]
NEEDED_FOR_CALC = ("inventory", "plan", "recipe")  # 입고 예정은 없어도 계산한다


def _norm(name: str) -> str:
    return re.sub(r"[\s_()\-\[\]]", "", str(name)).lower()


def _canonical_columns(kind: str) -> dict[str, str]:
    cols = REQUIRED[kind] + OPTIONAL[kind]
    out = {_norm(c): c for c in cols}
    out.update({_norm(c.replace("_kg", "")): c for c in cols if c.endswith("_kg")})  # '재고량'도 인정
    out.update({_norm(a): c for a, c in COLUMN_ALIASES.items() if c in cols})
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
    substitute: pd.DataFrame | None = None
    supplier: pd.DataFrame | None = None
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
        df["보관"] = df["보관"].replace({"nan": "", "None": "", "<NA>": ""}).fillna("")
        bad = sorted(set(df["보관"]) - ({"냉장", "냉동", ""} if kind != "inventory" else {"냉장", "냉동"}))
        if bad:
            issues.append(Issue("오류", label, f"보관은 냉장/냉동 중 하나여야 합니다: {', '.join(bad)}"))
            return None

    for c in [c for c in df.columns if c.endswith("_kg")] + [c for c in ("안전재고일수", "리드타임일수", "발주주기일", "해동일수", "해동감량률") if c in df.columns]:
        df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", "", regex=False), errors="coerce")
    for c in [c for c in df.columns if c.endswith("_kg")]:
        if kind == "supplier":  # 공급처의 수량 열은 비워도 된다
            df[c] = df[c].fillna(0.0 if c == "최소발주량_kg" else float(ROUND_KG))
        if df[c].isna().any() or not np.isfinite(df[c]).all():
            issues.append(Issue("오류", label, f"'{c}'에 숫자가 아닌 값이 있습니다 ({int(df[c].isna().sum())}행)"))
            return None
        if (df[c] < 0).any():
            issues.append(Issue("오류", label, f"'{c}'에 음수가 있습니다"))
            return None
    for c in [c for c in MONEY_COLUMNS if c in df.columns]:
        text = df[c].astype(str).str.replace(",", "", regex=False).str.replace("원", "", regex=False).str.strip()
        present = df[c].notna() & text.notna() & ~text.isin(["", "nan", "None", "<NA>"])
        df[c] = pd.to_numeric(text.where(present), errors="coerce")
        if (present & (df[c].isna() | ~np.isfinite(df[c].fillna(0)) | (df[c] <= 0))).any():
            issues.append(Issue("오류", label, f"'{c}'는 0보다 큰 숫자(원/kg)여야 합니다. 모르면 비워 두세요."))
            return None
    if "해동감량률" in df:
        raw_values = raw.rename(columns={x: canon.get(_norm(x), x) for x in raw.columns})["해동감량률"].reindex(df.index)
        present = raw_values.notna() & raw_values.astype(str).str.strip().ne("")
        if (present & (df["해동감량률"].isna() | (df["해동감량률"] < 0) | (df["해동감량률"] >= 50))).any():
            issues.append(Issue("오류", label, "해동감량률은 0~50 미만의 숫자(%)여야 합니다"))
            return None
    for c in ("안전재고일수", "리드타임일수", "발주주기일", "해동일수", "우선순위"):
        if c not in df:
            continue
        raw_values = raw.rename(columns={x: canon.get(_norm(x), x) for x in raw.columns})[c].reindex(df.index)
        present = raw_values.notna() & raw_values.astype(str).str.strip().ne("")
        df[c] = pd.to_numeric(df[c], errors="coerce")
        minimum = 1 if c in ("발주주기일", "우선순위") else 0
        maximum = {"안전재고일수": 90, "리드타임일수": 120, "발주주기일": 60, "해동일수": 14, "우선순위": 1000000}[c]
        if (present & (df[c].isna() | ~np.isfinite(df[c]) | (df[c] < minimum) | (df[c] > maximum) | (df[c] % 1 != 0))).any():
            issues.append(Issue("오류", label, f"{c}는 {minimum}~{maximum} 범위의 정수여야 합니다"))
            return None
    for c in [c for c in ("기준일", "입고예정일", "날짜", "발주일", "납기", "사용가능일", "사용기한") if c in df.columns]:
        present = df[c].notna() & ~df[c].astype(str).isin(["", "nan", "None", "NaT", "<NA>"])
        parsed = pd.to_datetime(df[c], errors="coerce")
        if (parsed.isna() & (present if c in ("발주일", "납기", "사용가능일", "사용기한") else True)).any():
            issues.append(Issue("오류", label, f"'{c}'의 날짜 형식이 올바르지 않습니다 (예: 2026-10-14)"))
            return None
        df[c] = parsed.dt.normalize()
    if kind in ("inventory", "inbound"):
        if "사용상태" not in df:
            df["사용상태"] = "사용가능"
            issues.append(Issue("참고", label, "기존 양식: 사용상태 미입력은 사용가능으로 가정합니다. QC·QA 확인 상태를 입력하세요."))
        if not df["사용상태"].isin(["사용가능", "검사대기", "사용보류"]).all():
            issues.append(Issue("오류", label, "사용상태는 사용가능/검사대기/사용보류 중 하나여야 합니다"))
            return None
        for c in ("사용가능일", "사용기한"):
            if c not in df:
                df[c] = pd.NaT
        if (df["사용기한"].notna() & df["사용가능일"].notna() & (df["사용가능일"] > df["사용기한"])).any():
            issues.append(Issue("오류", label, "사용가능일이 사용기한보다 늦습니다"))
            return None
        if df["사용기한"].isna().any():
            issues.append(Issue("참고", label, "사용기한 미입력 로트는 기한 제한 없이 계산합니다. 실제 사용 가능 여부를 보증하지 않습니다."))
    if kind == "plan":
        if "우선순위" not in df:
            df["우선순위"] = 100
        df["우선순위"] = df["우선순위"].fillna(100)
        if "납기" not in df:
            df["납기"] = pd.NaT
        if "주문번호" not in df:
            df["주문번호"] = "미지정"
        if df["제품"].isna().any() or df["제품"].isin(["", "nan", "None"]).any():
            issues.append(Issue("오류", label, "제품명이 비어 있습니다"))
            return None
    if kind == "substitute":
        df = _clean_substitute(df, issues, label)
        if df is None:
            return None
    if kind == "supplier":
        if (df["보관"] == "").any() or df["공급처"].isin(["", "nan", "None"]).any():
            issues.append(Issue("오류", label, "보관과 공급처 이름을 모두 적어 주세요"))
            return None
        if "발주단위_kg" in df and (df["발주단위_kg"] <= 0).any():
            issues.append(Issue("오류", label, "발주단위_kg는 0보다 커야 합니다"))
            return None
        for c in ("담당자", "연락처"):
            df[c] = df[c].replace({"nan": "", "None": ""}) if c in df else ""
        df["최소발주량_kg"] = df["최소발주량_kg"] if "최소발주량_kg" in df else 0.0
        df["발주단위_kg"] = df["발주단위_kg"] if "발주단위_kg" in df else float(ROUND_KG)
        df["우선순위"] = df["우선순위"].fillna(1) if "우선순위" in df else 1
    if kind == "recipe" and (df["투입량_kg"] <= 0).any():
        issues.append(Issue("오류", label, "배합표 투입량은 0보다 커야 합니다"))
        return None
    return df.reset_index(drop=True)


def _clean_substitute(df: pd.DataFrame, issues: list[Issue], label: str) -> pd.DataFrame | None:
    """대체표: (제품, 원래 원료) -> 대체 원료. 제품 '전체'·'*'·빈칸은 그 원료를 쓰는 모든 제품."""
    if (df["보관"] == "").any():
        issues.append(Issue("오류", label, "원래 원료의 보관(냉장/냉동)을 적어 주세요"))
        return None
    df["제품"] = df["제품"].astype(str).replace({"전체": "*", "nan": "*", "": "*", "None": "*"})
    if "대체육종" in df:
        sub_sp = [_std_species(v) if str(v) not in ("nan", "", "None") else o for v, o in zip(df["대체육종"], df["육종"])]
    else:
        sub_sp = list(df["육종"])
    df["대체육종"] = sub_sp
    bad = sorted(set(df["대체육종"]) - set(PARTS))
    if bad:
        issues.append(Issue("오류", label, f"알 수 없는 대체육종: {', '.join(bad)}"))
        return None
    df["대체부위"] = [_std_part(s_, p_) for s_, p_ in zip(df["대체육종"], df["대체부위"])]
    unknown = sorted({f"{s_} {p_}" for s_, p_ in zip(df["대체육종"], df["대체부위"]) if p_ not in PARTS[s_]})
    if unknown:
        issues.append(Issue("오류", label, "대체부위가 부위 목록에 없습니다: " + ", ".join(unknown)))
        return None
    df["대체원산지"] = df["대체원산지"].map(_std_origin)
    if not df["대체원산지"].isin(["국산", "수입"]).all() or not df["대체보관"].isin(["냉장", "냉동"]).all():
        issues.append(Issue("오류", label, "대체원산지는 국산/수입, 대체보관은 냉장/냉동이어야 합니다"))
        return None
    for c, default, low, high in (("최대대체비율", 100.0, 0.0, 100.0), ("환산계수", 1.0, 0.0, 5.0)):
        if c not in df:
            df[c] = default
            continue
        text = df[c].astype(str).str.replace("%", "", regex=False).str.strip()
        present = df[c].notna() & ~text.isin(["", "nan", "None", "<NA>"])
        v = pd.to_numeric(text.where(present), errors="coerce")
        if (present & (v.isna() | (v <= low) | (v > high))).any():
            issues.append(Issue("오류", label, f"{c}는 {low:g}보다 크고 {high:g} 이하인 숫자여야 합니다"))
            return None
        df[c] = v.where(present, default).astype(float)
    same = (df["육종"] == df["대체육종"]) & (df["부위"] == df["대체부위"]) & (df["원산지"] == df["대체원산지"]) & (df["보관"] == df["대체보관"])
    if same.any():
        issues.append(Issue("오류", label, "원래 원료와 대체 원료가 같은 행이 있습니다"))
        return None
    if "비고" not in df:
        df["비고"] = ""
    return df


def substitute_key(row) -> tuple:
    return (row["대체육종"], row["대체부위"], row["대체원산지"], row["대체보관"])


def load_data(raw_tables: dict[str, pd.DataFrame | None]) -> CompanyData:
    data = CompanyData()
    for kind in FILES:
        raw = raw_tables.get(kind)
        if raw is None:
            if kind in NEEDED_FOR_CALC:
                data.issues.append(Issue("오류", FILE_LABELS[kind], "파일이 없습니다"))
            continue
        setattr(data, kind, clean_table(kind, raw, data.issues))
    _resolve_storage(data)
    _cross_check(data)
    return data


def _resolve_storage(data: CompanyData) -> None:
    """옛 양식은 보관 후보가 하나일 때만 연결한다. 모호하면 계산을 막는다."""
    keys = MATERIAL_KEY[:3]
    for kind in ("recipe", "inbound"):
        df = getattr(data, kind)
        if df is None:
            continue
        if "보관" not in df:
            df["보관"] = ""
        for i, row in df.iterrows():
            if row["보관"] in ("냉장", "냉동"):
                continue
            candidates = set()
            for other in (data.inventory, data.recipe, data.inbound):
                if other is not None and "보관" in other:
                    mask = (other[keys] == row[keys]).all(axis=1)
                    candidates |= set(other.loc[mask, "보관"]) & {"냉장", "냉동"}
            if len(candidates) == 1:
                df.at[i, "보관"] = candidates.pop()
            else:
                data.issues.append(Issue("오류", FILE_LABELS[kind], f"{row['육종']} {row['부위']}({row['원산지']})의 보관을 명시하세요. 냉장·냉동을 임의 합산하지 않습니다."))


def _cross_check(d: CompanyData) -> None:
    if d.plan is None or d.recipe is None:
        return
    no_recipe = sorted(set(d.plan["제품"]) - set(d.recipe["제품"]))
    if no_recipe:
        d.issues.append(Issue("오류", "생산계획", "계산 불완전: 배합표가 없는 제품입니다. 발주 권고를 중단합니다: " + ", ".join(no_recipe)))
    if d.substitute is not None and len(d.substitute):
        uses = set(d.recipe[["제품"] + MATERIAL_KEY].itertuples(index=False, name=None))
        mats = {k[1:] for k in uses}
        dead = [f"{r['제품'] if r['제품'] != '*' else '전체'}: {ingredient_label(r['육종'], r['부위'], r['원산지'], r['보관'])}"
                for _, r in d.substitute.iterrows()
                if (r["제품"] == "*" and (r["육종"], r["부위"], r["원산지"], r["보관"]) not in mats)
                or (r["제품"] != "*" and (r["제품"], r["육종"], r["부위"], r["원산지"], r["보관"]) not in uses)]
        if dead:
            d.issues.append(Issue("참고", "대체표", "배합표에 없는 제품·원료 조합이라 쓰이지 않는 대체 규칙: " + "; ".join(dead)))
    unused = sorted(set(d.recipe["제품"]) - set(d.plan["제품"]))
    if unused:
        d.issues.append(Issue("참고", "배합표", "생산계획에 없는 제품(소요량 0): " + ", ".join(unused)))
    if d.inventory is not None:
        if d.inventory["기준일"].nunique() > 1:
            d.issues.append(Issue("오류", "재고", "모든 재고를 같은 기준일 마감으로 맞춰 주세요. 서로 다른 날짜의 재고는 합산하지 않습니다."))
        inv_keys = set(d.inventory[MATERIAL_KEY].itertuples(index=False, name=None))
        need = set(d.recipe[MATERIAL_KEY].itertuples(index=False, name=None))
        no_stock = sorted(ingredient_label(*key) for key in need - inv_keys)
        if no_stock:
            d.issues.append(Issue("경고", "재고", "배합표에 쓰이지만 재고가 없는 원료(재고 0으로 계산): " + ", ".join(no_stock)))
        cols = ["기준일", "육종", "부위", "원산지", "보관"] + (["로트"] if "로트" in d.inventory else [])  # 로트가 다르면 중복이 아니다
        dup = d.inventory.duplicated(cols, keep=False)
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
    actionable: bool = True
    reference_date: pd.Timestamp | None = None


@dataclass
class Settings:
    """CSV에 값이 없을 때 쓰는 기본값. 화면에서 바꿀 수 있다."""
    safety: dict = field(default_factory=lambda: dict(DEFAULT_SAFETY))
    lead: dict = field(default_factory=lambda: dict(DEFAULT_LEAD))
    cycle: dict = field(default_factory=lambda: dict(DEFAULT_CYCLE))
    thaw_days: dict = field(default_factory=lambda: dict(DEFAULT_THAW_DAYS))
    thaw_loss: dict = field(default_factory=lambda: dict(DEFAULT_THAW_LOSS))
    thaw_capacity: int = 0  # 해동실 하루 최대 투입량(kg, 냉동 무게). 0 = 제한 없음
    thaw_weekdays: list = field(default_factory=lambda: list(DEFAULT_THAW_WEEKDAYS))
    holidays: list = field(default_factory=list)   # 'YYYY-MM-DD' 휴무일 (해동 투입 불가)
    thaw_hold: int = DEFAULT_THAW_HOLD
    spot_premium: float = DEFAULT_SPOT_PREMIUM
    spot_lead: dict = field(default_factory=lambda: dict(DEFAULT_SPOT_LEAD))
    expiry_warn: dict = field(default_factory=lambda: dict(DEFAULT_EXPIRY_WARN))
    shelf: dict = field(default_factory=lambda: dict(DEFAULT_SHELF))
    disposal_cost: float = 0.0  # 폐기 처리비(원/kg)

    def can_start(self, day: pd.Timestamp) -> bool:
        """해동실에 넣을 수 있는 날인지 (작업 요일이고 휴무일이 아님)."""
        return day.weekday() in self.thaw_weekdays and f"{day:%Y-%m-%d}" not in self.holidays

    def next_start(self, day: pd.Timestamp, limit: int = 60) -> pd.Timestamp | None:
        for i in range(limit):
            d = day + pd.Timedelta(days=i)
            if self.can_start(d):
                return d
        return None


def today_kst() -> pd.Timestamp:
    return pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).normalize()


def freshness(data: CompanyData, today: pd.Timestamp) -> str:
    base = data.inventory["기준일"].max()
    age = (pd.Timestamp(today).normalize() - base).days
    if age < 0:
        return "재고 기준일이 미래입니다. 현재 발주 판단을 중단합니다."
    if age > 1:
        return f"재고 기준일이 {age}일 전입니다. 최신 마감 재고·실제 입고·생산계획으로 갱신하기 전에는 발주 권고를 하지 않습니다."
    return ""


def load_settings() -> Settings:
    path = COMPANY_DIR / "settings.json"
    if not path.exists():
        return Settings()
    value = json.loads(path.read_text(encoding="utf-8"))
    result = Settings(safety={tuple(k.split("/")): v for k, v in value["safety"].items()}, lead=value["lead"], cycle=value["cycle"],
                      thaw_days=value.get("thaw_days", dict(DEFAULT_THAW_DAYS)), thaw_loss=value.get("thaw_loss", dict(DEFAULT_THAW_LOSS)),
                      thaw_capacity=value.get("thaw_capacity", 0), thaw_weekdays=value.get("thaw_weekdays", list(DEFAULT_THAW_WEEKDAYS)),
                      holidays=value.get("holidays", []), thaw_hold=value.get("thaw_hold", DEFAULT_THAW_HOLD),
                      spot_premium=value.get("spot_premium", DEFAULT_SPOT_PREMIUM), spot_lead=value.get("spot_lead", dict(DEFAULT_SPOT_LEAD)),
                      expiry_warn=value.get("expiry_warn", dict(DEFAULT_EXPIRY_WARN)), shelf=value.get("shelf", dict(DEFAULT_SHELF)),
                      disposal_cost=value.get("disposal_cost", 0.0))  # 예전 설정 파일에는 해동 값이 없다
    _validate_settings(result)
    return result


def _validate_settings(settings: Settings) -> None:
    for values, defaults, low, high in ((settings.safety, DEFAULT_SAFETY, 0, 90), (settings.lead, DEFAULT_LEAD, 0, 120), (settings.cycle, DEFAULT_CYCLE, 1, 60)):
        if set(values) != set(defaults) or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v != int(v) or not low <= v <= high for v in values.values()):
            raise ValueError("저장된 기본값이 올바르지 않습니다. 설정 파일을 확인하세요.")
    td, tl, cap = settings.thaw_days, settings.thaw_loss, settings.thaw_capacity
    if set(td) != set(DEFAULT_THAW_DAYS) or any(not isinstance(v, (int, float)) or v != int(v) or not 0 <= v <= 14 for v in td.values()):
        raise ValueError("해동일수 기본값은 0~14 정수여야 합니다.")
    if set(tl) != set(DEFAULT_THAW_LOSS) or any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v < 50 for v in tl.values()):
        raise ValueError("해동감량률 기본값은 0~50 미만이어야 합니다.")
    if not isinstance(cap, (int, float)) or cap != int(cap) or not 0 <= cap <= 1_000_000:
        raise ValueError("해동실 용량은 0~1,000,000kg 정수여야 합니다.")
    wd = settings.thaw_weekdays
    if not isinstance(wd, list) or not wd or any(not isinstance(v, int) or not 0 <= v <= 6 for v in wd) or len(set(wd)) != len(wd):
        raise ValueError("해동 투입 요일을 하나 이상 골라 주세요.")
    if not isinstance(settings.holidays, list) or any(not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in settings.holidays):
        raise ValueError("휴무일은 2026-10-09 형식이어야 합니다.")
    for v in settings.holidays:
        pd.Timestamp(v)  # 없는 날짜면 오류
    if not isinstance(settings.thaw_hold, int) or not 0 <= settings.thaw_hold <= 14:
        raise ValueError("해동 후 보관 한도는 0~14일 정수여야 합니다.")
    if not isinstance(settings.spot_premium, (int, float)) or not math.isfinite(settings.spot_premium) or not 0 <= settings.spot_premium <= 200:
        raise ValueError("긴급 현물 할증은 0~200%여야 합니다.")
    if set(settings.spot_lead) != set(DEFAULT_SPOT_LEAD) or any(not isinstance(v, int) or not 0 <= v <= 30 for v in settings.spot_lead.values()):
        raise ValueError("긴급 현물 도착 일수는 0~30 정수여야 합니다.")
    for values, defaults, high, what in ((settings.expiry_warn, DEFAULT_EXPIRY_WARN, 365, "임박 기준"), (settings.shelf, DEFAULT_SHELF, 1095, "가정 기한")):
        if set(values) != set(defaults) or any(not isinstance(v, int) or not 0 <= v <= high for v in values.values()):
            raise ValueError(f"{what}은 0~{high}일 정수여야 합니다.")
    if not isinstance(settings.disposal_cost, (int, float)) or not math.isfinite(settings.disposal_cost) or not 0 <= settings.disposal_cost <= 100000:
        raise ValueError("폐기 처리비는 0~100,000원/kg이어야 합니다.")


def save_settings(settings: Settings) -> None:
    _validate_settings(settings)
    COMPANY_DIR.mkdir(parents=True, exist_ok=True)
    target = COMPANY_DIR / "settings.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps({"safety": {"/".join(k): v for k, v in settings.safety.items()}, "lead": settings.lead, "cycle": settings.cycle,
                                "thaw_days": settings.thaw_days, "thaw_loss": settings.thaw_loss, "thaw_capacity": int(settings.thaw_capacity),
                                "thaw_weekdays": sorted(settings.thaw_weekdays), "holidays": sorted(settings.holidays), "thaw_hold": settings.thaw_hold,
                                "spot_premium": float(settings.spot_premium), "spot_lead": settings.spot_lead,
                                "expiry_warn": settings.expiry_warn, "shelf": settings.shelf, "disposal_cost": float(settings.disposal_cost)}, ensure_ascii=False), encoding="utf-8")
    temp.replace(target)


def _share_text(d: dict) -> str:
    tot = sum(d.values())
    if tot <= 0:
        return ""
    return " · ".join(f"{p} {v / tot * 100:.0f}%" for p, v in sorted(d.items(), key=lambda x: -x[1]))


def ingredient_label(species: str, part: str, origin: str, storage: str = "") -> str:
    return f"{species} {part}({origin})" + (f" · {storage}" if storage else "")


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


def lot_thaw(r, origin: str, storage: str, settings: Settings) -> tuple[int, float]:
    """(해동일수, 감량률%) — 냉장은 (0, 0). CSV 값이 있으면 그 값, 없으면 기본값."""
    if storage != "냉동":
        return 0, 0.0
    days_ = r.get("해동일수") if hasattr(r, "get") else None
    loss = r.get("해동감량률") if hasattr(r, "get") else None
    days_ = int(days_) if days_ is not None and pd.notna(days_) else int(settings.thaw_days[origin])
    loss = float(loss) if loss is not None and pd.notna(loss) else float(settings.thaw_loss[origin])
    return days_, loss


def material_lots(data: CompanyData, settings: Settings | None = None) -> list[dict]:
    """입고·검사 상태·해동·사용기한을 반영한 로트. 품질 판정은 입력값만 사용한다.

    냉동 로트: 수량은 해동 후 무게(냉동 무게 x (1 - 감량률))로 쓴다. 사용가능일을 적었으면 그날(이미 해동 중인 로트),
    안 적었으면 들어온 날(재고는 기준일 다음 날) 해동을 시작해 해동일수 뒤부터 쓴다고 본다.
    """
    st_ = settings or Settings()
    base = data.inventory["기준일"].max()
    lots = []
    for kind, df, qty in (("inventory", data.inventory, "재고량_kg"), ("inbound", data.inbound, "입고량_kg")):
        if df is None:
            continue
        for i, r in df.iterrows():
            arrival = base + pd.Timedelta(days=1) if kind == "inventory" else r["입고예정일"]
            if kind == "inbound" and arrival <= base:
                continue
            key = tuple(r[c] for c in MATERIAL_KEY)
            thaw_days, loss = lot_thaw(r, key[2], key[3], st_)
            explicit = pd.notna(r["사용가능일"])
            expires = r["사용기한"]
            if explicit:
                ready = max(arrival, r["사용가능일"])
                if thaw_days:  # 이미 해동 중인 냉동 로트는 해동 후 보관 한도 안에 써야 한다
                    hold_end = ready + pd.Timedelta(days=int(st_.thaw_hold))
                    expires = hold_end if pd.isna(expires) else min(pd.Timestamp(expires), hold_end)
            elif thaw_days:
                start = st_.next_start(arrival)  # 휴무일·주말에는 해동실에 넣지 않는다
                ready = (start + pd.Timedelta(days=thaw_days)) if start is not None else pd.Timestamp.max
            else:
                ready = arrival
            lots.append({"key": key, "id": str(r.get("로트", f"{kind}-{i}")),
                         "qty": float(r[qty]) * (1 - loss / 100), "frozen_qty": float(r[qty]), "ready": ready, "arrival": arrival,
                         "expires": expires, "status": r["사용상태"], "kind": kind,
                         "thaw_days": thaw_days, "loss": loss, "explicit_ready": bool(explicit),
                         "price": float(r["단가_원kg"]) if "단가_원kg" in r and pd.notna(r["단가_원kg"]) else math.nan})
    return lots


def available_lots(lots: list[dict], key: tuple, day: pd.Timestamp) -> list[dict]:
    return sorted((lot for lot in lots if lot["key"] == key and lot["status"] == "사용가능" and lot["ready"] <= day
                   and (pd.isna(lot["expires"]) or day <= lot["expires"])),
                  key=lambda lot: (lot["expires"] if pd.notna(lot["expires"]) else pd.Timestamp.max, lot["ready"], lot["id"]))


def take(lots: list[dict], key: tuple, day: pd.Timestamp, quantity: float) -> float:
    remaining = max(quantity, 0.0)
    for lot in available_lots(lots, key, day):
        used = min(lot["qty"], remaining)
        lot["qty"] -= used
        remaining -= used
    return remaining


def project(data: CompanyData, horizon: int = HORIZON_DAYS, today: pd.Timestamp | None = None,
            settings: Settings | None = None) -> Projection:
    if not data.ready():
        raise ValueError("계산 불완전: 입력 오류를 먼저 수정해 주세요.")
    st_ = settings or Settings()
    _validate_settings(st_)
    inv, inb, plan, rec = data.inventory, data.inbound, data.plan, data.recipe
    notes: list[str] = []
    base = inv["기준일"].max()
    ref = base if today is None else max(pd.Timestamp(today).normalize(), base)
    dates = pd.date_range(base + timedelta(days=1), periods=horizon)

    pd_daily = _plan_daily(plan, base, dates, notes)
    key = MATERIAL_KEY
    r = rec.groupby(["제품"] + key)["투입량_kg"].sum().reset_index()
    demand, prod_dem = {}, {}
    for _, row in r.iterrows():
        if row["제품"] not in pd_daily.columns:
            continue
        k = tuple(row[c] for c in key)
        demand.setdefault(k, pd.Series(0.0, index=dates))
        demand[k] = demand[k] + pd_daily[row["제품"]] * row["투입량_kg"]
        prod_dem.setdefault(k, {})[row["제품"]] = prod_dem.get(k, {}).get(row["제품"], 0.0) + float((pd_daily[row["제품"]].iloc[:14] * row["투입량_kg"]).sum())

    keys = set(demand) | set(inv[key].itertuples(index=False, name=None))
    if inb is not None and len(inb):
        keys |= set(inb[key].itertuples(index=False, name=None))

    inbound_by = {}
    if inb is not None and len(inb):
        past = inb[inb["입고예정일"] <= base]
        if len(past):
            notes.append(f"기준일({base:%m/%d}) 이전 입고 예정 {len(past)}건은 이미 재고에 반영된 것으로 보고 제외했습니다")
        fut = inb[inb["입고예정일"] > base]
        for k, g in fut.groupby(key):
            inbound_by[k] = g.groupby("입고예정일")["입고량_kg"].sum().reindex(dates, fill_value=0.0)

    rows, daily_parts = [], []
    all_lots = material_lots(data, st_)
    stale = freshness(data, today) if today is not None else ""
    if stale:
        notes.append(stale)
    for k in sorted(keys):
        species, part, origin, storage = k
        g_inv = inv[(inv[key] == pd.Series(k, index=key)).all(axis=1)]
        if len(g_inv):
            last_day = g_inv["기준일"].max()
            cur = g_inv[g_inv["기준일"] == last_day]
            s0 = float(cur["재고량_kg"].sum())
            if last_day < base:
                notes.append(f"{ingredient_label(*k)} 재고 기준일이 {(base - last_day).days}일 전({last_day:%m/%d})입니다")
        else:
            s0 = 0.0
        def _col(name, default):
            return float(g_inv[name].max()) if len(g_inv) and name in g_inv and g_inv[name].notna().any() else float(default)
        safety_days = _col("안전재고일수", st_.safety[(origin, storage)])
        lead_days = _col("리드타임일수", st_.lead[origin])
        cycle_days = _col("발주주기일", st_.cycle[origin])
        dem = demand.get(k, pd.Series(0.0, index=dates))
        inc = inbound_by.get(k, pd.Series(0.0, index=dates))
        lots = [dict(lot) for lot in all_lots if lot["key"] == k]
        usable0 = sum(lot["qty"] for lot in available_lots(lots, k, dates[0]) if lot["kind"] == "inventory")
        # 재고일수는 해동 대기 중인 냉동 재고까지 포함한 보유분으로 센다 (첫날 사용 가능분과 별도)
        hold0 = sum(lot["qty"] for lot in lots if lot["kind"] == "inventory" and lot["status"] == "사용가능"
                    and (pd.isna(lot["expires"]) or lot["expires"] >= dates[0]))
        thaw_t, thaw_loss = lot_thaw(g_inv.iloc[0] if len(g_inv) else {}, origin, storage, st_)
        missing, balances = 0.0, []
        for day in dates:
            missing += take(lots, k, day, float(dem.loc[day]))
            balances.append(sum(lot["qty"] for lot in available_lots(lots, k, day)) - missing)
        stock = pd.Series(balances, index=dates)
        avg7 = float(dem.iloc[:7].mean())
        safety_qty = safety_days * avg7

        cum_dem = dem.cumsum()
        run_no_inbound = cum_dem[cum_dem > hold0]
        if avg7 <= 0:
            cover_days = math.inf
        elif len(run_no_inbound):
            i = int((cum_dem > hold0).values.argmax())
            prev = float(cum_dem.iloc[i - 1]) if i > 0 else 0.0
            cover_days = i + (hold0 - prev) / max(float(dem.iloc[i]), 1e-9)
        else:
            cover_days = math.inf
        neg = stock[stock < 0]
        runout = neg.index[0] if len(neg) else None
        below = stock[stock < safety_qty - 1e-9] if safety_qty > 0 else stock[stock < 0]
        breach = below.index[0] if len(below) else None
        arrival = ref + timedelta(days=int(lead_days))
        start_at = st_.next_start(arrival) if thaw_t else arrival  # 냉동은 들어온 뒤 해동 가능한 날 넣어 해동일수가 지나야 쓴다
        ready_at = (start_at or arrival) + timedelta(days=int(thaw_t))

        status, order_qty, order_by, gap = "정상", 0.0, None, 0.0
        under_days, under_end = 0, None
        if breach is not None:
            eff = max(breach, ready_at)
            at_arrival = float(stock.loc[eff]) if eff in stock.index else float(stock.iloc[-1])
            target = safety_qty + cycle_days * avg7  # 도착 시점에 맞춰 둘 목표 재고
            order_qty = max(target - at_arrival, 0.0) / (1 - thaw_loss / 100)  # 해동 후 필요량 -> 사야 할 냉동 무게
            order_qty = math.ceil(order_qty / ROUND_KG) * ROUND_KG if order_qty > 0 else 0.0
            order_by = max(eff - timedelta(days=int(lead_days + thaw_t)), ref)
            pre = stock[stock.index < ready_at]
            gap = float(max(-pre.min(), 0.0)) if len(pre) else 0.0
            low = pre[pre < safety_qty - 1e-9]
            under_days = int(len(low))                        # 입고 전 안전재고 아래로 지내는 실제 일수 (이미 잡힌 입고로 회복되면 그만큼 뺀다)
            under_end = low.index[-1] if len(low) else None
            if runout is not None and runout < ready_at:
                status = "긴급"
            elif order_qty > 0 and (order_by - ref).days <= cycle_days:
                status = "발주필요"  # 이번 발주 주기 안에 발주해야 하는 것만 '할 일'로 올린다 (더 먼 것은 발주 기한만 보여준다)
        if status == "정상" and avg7 > 0:
            over_days = max(safety_days * 4, 20)
            if (hold0 + float(inc.sum()) * (1 - thaw_loss / 100)) / avg7 > over_days:
                status = "과잉"
        if breach is not None and max(breach, ready_at) > dates[-1]:
            status, order_qty = "확인필요", 0.0
            notes.append(f"{ingredient_label(*k)}의 예상 입고가 {horizon}일 계산 범위를 벗어나 발주량 권고를 중단했습니다")

        rows.append({
            "육종": species, "부위": part, "원산지": origin, "보관": storage, "원료": ingredient_label(*k),
            "현재고_kg": s0, "초기사용가능_kg": usable0, "일평균사용_kg": avg7, "재고일수": cover_days, "안전재고일수": safety_days,
            "리드타임일수": lead_days, "소진예정일": runout, "안전재고이하일": breach, "상태": "확인필요" if stale else status,
            "발주기한": order_by, "발주량_kg": order_qty, "공백량_kg": gap, "가장빠른입고일": arrival, "가장빠른사용가능일": ready_at, "해동일수": thaw_t, "해동감량률": thaw_loss, "안전재고미달일수": under_days, "안전재고미달종료일": under_end, "발주주기일": cycle_days, "영향제품": _share_text(prod_dem.get(k, {})), "입고예정_kg": float(inc.sum()),
        })
        daily_parts.append(pd.DataFrame({"원료": ingredient_label(*k), "날짜": dates, "예상재고_kg": stock.values,
                                          "소요량_kg": dem.values, "입고_kg": inc.values, "안전재고_kg": safety_qty}))

    summary = pd.DataFrame(rows)
    order = {"확인필요": -1, "긴급": 0, "발주필요": 1, "과잉": 2, "정상": 3}
    if len(summary):
        summary = summary.sort_values(["상태", "재고일수"], key=lambda s: s.map(order) if s.name == "상태" else s).reset_index(drop=True)
    daily = pd.concat(daily_parts, ignore_index=True) if daily_parts else pd.DataFrame()
    return Projection(base_date=base, summary=summary, daily=daily, notes=notes, actionable=not stale, reference_date=ref)


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
    t = pd.Timestamp(today if today is not None else today_kst()).normalize()
    d = lambda n: (t + timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    inventory = pd.DataFrame([
        [d(0), "돼지", "앞다리", "국산", "냉장", 9000, None, None],
        [d(0), "돼지", "삼겹살", "국산", "냉장", 5200, None, None],
        [d(0), "돼지", "뒷다리", "수입", "냉동", 6000, None, None],   # 해동실에 들어가 있는 로트 (사용가능일 = 내일)
        [d(0), "돼지", "뒷다리", "수입", "냉동", 36000, None, None],  # 냉동 창고 로트 (해동 일정을 계산)
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
    storage_map = {(r['육종'], r['부위'], r['원산지']): r['보관'] for _, r in inventory.iterrows()}
    for frame in (inbound, recipe):
        frame["보관"] = [storage_map[tuple(r[c] for c in MATERIAL_KEY[:3])] for _, r in frame.iterrows()]
    for kind, frame in (("ST", inventory), ("IN", inbound)):
        frame["로트"] = [f"{kind}-{i+1:03d}" for i in range(len(frame))]
        frame["사용상태"] = "사용가능"
        frame["사용가능일"] = d(1) if kind == "ST" else frame["입고예정일"]
        frame.loc[frame["보관"] == "냉동", "사용가능일"] = None  # 냉동은 해동일수로 계산
        frame["사용기한"] = d(60)
        frame.loc[frame["보관"] == "냉동", "사용기한"] = d(365)  # 냉동은 기한이 길다
    inventory.loc[inventory["로트"].isin(["ST-003", "ST-005"]), "사용가능일"] = d(1)  # 뒷다리 6톤·양지는 이미 해동 중
    inventory.loc[inventory["로트"] == "ST-007", "사용기한"] = d(20)  # 사태: 계획 뒤 평균 사용량으로는 기한까지 다 못 쓰는 예시
    # 단가·판매가도 설명용 가짜 값이다 (원/kg)
    inventory["단가_원kg"] = [5500, 13000, 4200, 4200, 9500, 9000, 17000]
    inbound["단가_원kg"] = [5900, 10200]
    plan["주문번호"] = [f"DEMO-{i+1:03d}" for i in range(len(plan))]
    plan["납기"] = plan["날짜"]
    plan["우선순위"] = 100
    plan["판매가_원kg"] = plan["제품"].map({"프레스햄": 9000, "소시지": 8500, "베이컨": 21000, "육개장": 9000, "양념갈비": 14000})
    # 대체표(설명용): 육개장의 수입 냉동 양지를 국산 냉장 사태로 최대 50%, 프레스햄의 수입 냉동 뒷다리를 국산 냉장 앞다리로 최대 40%
    substitute = pd.DataFrame([
        ["육개장", "소", "양지", "수입", "냉동", "사태", "국산", "냉장", 50, 1.0, "식감 확인 필요"],
        ["프레스햄", "돼지", "뒷다리", "수입", "냉동", "앞다리", "국산", "냉장", 40, 1.0, ""],
    ], columns=["제품", "육종", "부위", "원산지", "보관", "대체부위", "대체원산지", "대체보관", "최대대체비율", "환산계수", "비고"])
    # 공급처(설명용 가짜 이름·연락처)
    supplier = pd.DataFrame([
        ["돼지", "앞다리", "국산", "냉장", "A축산", "구매 담당", "000-0000-0000", 500, 100, 1],
        ["돼지", "삼겹살", "국산", "냉장", "A축산", "구매 담당", "000-0000-0000", 300, 100, 1],
        ["돼지", "갈비", "국산", "냉장", "A축산", "구매 담당", "000-0000-0000", 300, 100, 1],
        ["돼지", "뒷다리", "수입", "냉동", "B무역", "수입 담당", "000-0000-0001", 1000, 20, 1],
        ["소", "양지", "수입", "냉동", "B무역", "수입 담당", "000-0000-0001", 1000, 20, 1],
        ["소", "사태", "국산", "냉장", "C한우", "영업 담당", "000-0000-0002", 100, 10, 1],
    ], columns=["육종", "부위", "원산지", "보관", "공급처", "담당자", "연락처", "최소발주량_kg", "발주단위_kg", "우선순위"])
    return {"inventory": inventory, "inbound": inbound, "plan": plan, "recipe": recipe, "substitute": substitute, "supplier": supplier}


def part_list_text() -> str:
    lines = ["# 부위목록 (육종별로 이 이름 중에서 입력)"]
    for sp, parts in PARTS.items():
        lines.append(f"{sp}: " + ", ".join(parts))
    return "\n".join(lines)
