"""Streamlit Community Cloud용 샘플 전용 진입점."""

import os
import runpy
from pathlib import Path


os.environ["SUPPLY_PUBLIC_DEMO"] = "1"
runpy.run_path(str(Path(__file__).with_name("app.py")), run_name="__main__")
