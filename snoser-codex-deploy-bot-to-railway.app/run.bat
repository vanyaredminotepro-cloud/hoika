@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Warlord RP Bot...
python -m app.warlord.main
pause