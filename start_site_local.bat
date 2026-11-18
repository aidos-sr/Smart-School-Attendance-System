@echo off
rem ТОЛЬКО ДЛЯ ПРОБЫ в локальной сети. Для настоящего сайта задайте свои секреты (см. README_SITE_RU.md).
setlocal
cd /d "%~dp0"
set API_KEY=local-test-key-change-me
set VIEW_PASSWORD=admin12345
set SECRET_KEY=local-secret-change-me
set COOKIE_SECURE=0
py -3.12 -m pip install -r requirements.txt
py -3.12 server.py
pause
