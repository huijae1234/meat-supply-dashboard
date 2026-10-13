@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv (
  echo [1/3] 가상환경 생성
  python -m venv .venv || (echo Python 3.11+ 이 필요합니다 & exit /b 1)
)
echo [2/3] 패키지 설치
.venv\Scripts\python -m pip install -q -r requirements.txt || exit /b 1
if not exist .env (
  copy .env.example .env >nul
  echo [3/3] .env 를 만들었습니다. 메모장으로 열어 인증키를 입력하세요.
) else (
  echo [3/3] .env 가 이미 있습니다.
)
echo 완료. 다음: .venv\Scripts\python collect.py  그리고  run.bat
