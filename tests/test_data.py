"""합성 CSV 계층: 소비량 입력 오류 보정."""
import pandas as pd

from lib.data import OUTLIER_RATIO, _clean_meat_weekly


def _frame(values):
    n = len(values)
    return pd.DataFrame({
        "week_start": pd.date_range("2026-01-05", periods=n, freq="W-MON"),
        "species": "pork", "region_id": "R01", "cut_id": "P01", "channel_id": "C1", "origin": "domestic",
        "consumption_kg": values, "sales_krw": [v * 10000 for v in values],
        "wholesale_price_krw_kg": 5000, "retail_price_krw_kg": 10000, "data_type": "actual",
    })


def test_outlier_replaced_by_group_median_and_reported():
    base = [100.0] * 9 + [100.0 * (OUTLIER_RATIO + 8)]  # 마지막 값이 입력 오류(12배)
    cleaned, errors = _clean_meat_weekly(_frame(base))
    assert len(errors) == 1
    assert cleaned["consumption_kg"].iloc[-1] == 100.0
    assert cleaned["consumption_raw_kg"].iloc[-1] == 1200.0  # 원본은 보존
    assert cleaned["sales_krw"].iloc[-1] == 100.0 * 10000  # 판매액도 재계산


def test_normal_values_untouched():
    cleaned, errors = _clean_meat_weekly(_frame([100.0, 120.0, 90.0, 110.0, 105.0]))
    assert errors.empty
    assert cleaned["consumption_kg"].tolist() == [100.0, 120.0, 90.0, 110.0, 105.0]
