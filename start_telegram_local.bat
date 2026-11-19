@echo off
setlocal
cd /d "%~dp0"
py -3 telegram_worker.py --prompt
pause
