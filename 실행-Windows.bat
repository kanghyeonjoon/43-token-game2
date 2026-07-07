@echo off
rem 윈도우용 실행 파일 — 더블클릭하면 트렌드 뷰어가 켜지고 브라우저가 열립니다.
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [알림] 파이썬^(Python 3^)이 설치되어 있지 않습니다.
  echo   https://www.python.org/downloads/ 에서 설치한 뒤 이 파일을 다시 더블클릭해 주세요.
  echo   ^(설치 화면에서 "Add Python to PATH" 를 꼭 체크하세요.^)
  echo.
  pause
  exit /b 1
)

echo.
echo   ▶ 데일리 트렌드 뷰어를 시작합니다...
echo   ▶ 잠시 후 브라우저가 자동으로 열립니다. ^(안 열리면 http://localhost:8778 접속^)
echo   ▶ 종료하려면 이 창을 닫거나 Ctrl+C 를 누르세요.
echo.

rem 2초 뒤 브라우저 자동 오픈
start "" cmd /c "timeout /t 2 /nobreak >nul & start "" http://localhost:8778"

python server.py
pause
