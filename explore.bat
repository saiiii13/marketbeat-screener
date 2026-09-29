@echo off
title MarketBeat explore
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python run.py explore
echo.
pause
