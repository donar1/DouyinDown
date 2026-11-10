@echo off
chcp 65001 >nul
cd /d "%~dp0"
title DouyinDown

if exist "runtime\python.exe" (
  set "PY=runtime\python.exe"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python not found. Please unzip the full release package.
    pause
    exit /b 1
  )
  set "PY=python"
)

echo Starting DouyinDown...
"%PY%" run.py
if errorlevel 1 pause
