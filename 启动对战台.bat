@echo off
title AI PK - Battle Panel
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python not found. Please install Python 3.10 or newer first:
  echo     https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

echo Starting the AI PK panel ... ^(keep this window open; press Ctrl+C to stop^)
echo.
python -m aipk panel --port 8771

echo.
echo Panel stopped.
pause
