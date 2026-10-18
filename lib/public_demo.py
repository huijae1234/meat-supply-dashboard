"""공개 배포에서 실제 회사 파일을 사용하지 않는 샘플 전용 모드."""

import os


def enabled() -> bool:
    return os.environ.get("SUPPLY_PUBLIC_DEMO") == "1"
