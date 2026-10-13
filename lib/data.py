"""데이터 소스 계층. 대시보드는 DataSource 인터페이스만 사용한다.

1단계: CsvSource (data/*.csv)
2단계: PublicDataApiSource (공공데이터포털 OpenAPI) - 키 발급 후 fetch_* 구현
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SPECIES_KO = {"pork": "돼지고기", "beef": "쇠고기"}
OUTLIER_RATIO = 4.0  # 그룹 중앙값 대비 N배 초과 소비량 = 입력 오류로 간주


class DataSource(ABC):
    """테이블 이름 -> DataFrame. 실제 API로 교체할 때 이 클래스만 구현한다."""

    name = "base"

    @abstractmethod
    def read(self, table: str) -> pd.DataFrame: ...

    def signature(self) -> str:
        return self.name


class CsvSource(DataSource):
    name = "로컬 CSV"

    def __init__(self, folder: Path = DATA_DIR):
        self.folder = folder

    def read(self, table: str) -> pd.DataFrame:
        return pd.read_csv(self.folder / f"{table}.csv")

    def signature(self) -> str:
        files = sorted(self.folder.glob("*.csv"))
        return "|".join(f"{f.name}:{f.stat().st_mtime_ns}" for f in files)


class PublicDataApiSource(DataSource):
    """공공데이터포털(data.go.kr) OpenAPI 연동 자리.

    키는 환경변수 DATA_GO_KR_KEY 로 받는다. 구체 API가 정해지면 read()에서
    테이블별로 호출하고 CsvSource와 같은 컬럼 스키마로 맞춰 반환하면 된다.
    호출 결과는 data/cache/ 에 TTL 캐시하는 것을 권장한다.
    """

    name = "공공데이터포털 API"

    def __init__(self):
        self.key = os.environ.get("DATA_GO_KR_KEY", "")

    def read(self, table: str) -> pd.DataFrame:
        raise NotImplementedError(
            f"'{table}' 테이블용 API가 아직 연결되지 않았습니다. 키 발급 후 lib/data.py 에서 구현하세요."
        )


def get_source() -> DataSource:
    kind = os.environ.get("DATA_SOURCE", "csv").lower()
    return PublicDataApiSource() if kind == "api" else CsvSource()


def _clean_meat_weekly(m: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """소비량 입력 오류를 그룹 중앙값으로 보정하고 오류 행 목록을 함께 돌려준다."""
    keys = ["species", "region_id", "cut_id", "channel_id", "origin"]
    med = m.groupby(keys)["consumption_kg"].transform("median")
    bad = m["consumption_kg"] > med * OUTLIER_RATIO
    errors = m.loc[bad, ["week_start", *keys, "consumption_kg"]].copy()
    errors["보정값"] = med[bad].round(1)
    m = m.copy()
    m["consumption_raw_kg"] = m["consumption_kg"]
    m.loc[bad, "consumption_kg"] = med[bad]
    # 판매액은 보정 소비량 x 소매가로 재계산 (소비량이 틀린 행만)
    fix = bad & m["retail_price_krw_kg"].notna()
    m.loc[fix, "sales_krw"] = (m.loc[fix, "consumption_kg"] * m.loc[fix, "retail_price_krw_kg"]).round()
    return m, errors


def load_all(src: DataSource) -> dict:
    t: dict = {}
    for name in ["fact_supply_weekly", "fact_import_monthly", "fact_distribution_flow_monthly", "dim_calendar"]:
        t[name] = src.read(name)
    for name in ["dim_region", "dim_cut", "dim_channel"]:
        t[name] = src.read(name)

    sup = t["fact_supply_weekly"]
    sup["week_start"] = pd.to_datetime(sup["week_start"])
    sup["total_supply_kg"] = sup["domestic_output_kg"] + sup["import_volume_kg"]
    # 재고가 몇 주치 공급량에 해당하는지 (재고 회전 지표)
    sup["weeks_of_cover"] = sup["ending_stock_kg"] / sup["total_supply_kg"]
    t["fact_supply_weekly"] = sup

    imp = t["fact_import_monthly"]
    imp["month"] = pd.to_datetime(imp["month"])
    imp["weekly_avg_kg"] = imp["import_volume_kg"] / imp["weeks_in_month"]  # 4주/5주 달 보정
    t["fact_import_monthly"] = imp

    flow = t["fact_distribution_flow_monthly"]
    flow["month"] = pd.to_datetime(flow["month"])
    flow["weekly_avg_kg"] = flow["volume_kg"] / flow["weeks_in_month"]
    t["fact_distribution_flow_monthly"] = flow

    cal = t["dim_calendar"]
    for c in ["week_start", "week_end", "month"]:
        cal[c] = pd.to_datetime(cal[c])
    t["dim_calendar"] = cal

    m = src.read("fact_meat_weekly")
    m["week_start"] = pd.to_datetime(m["week_start"])
    m, errors = _clean_meat_weekly(m)
    t["fact_meat_weekly"] = m
    t["meat_errors"] = errors
    return t


def last_actual_week(sup: pd.DataFrame) -> pd.Timestamp:
    return sup.loc[sup["data_type"] == "actual", "week_start"].max()
