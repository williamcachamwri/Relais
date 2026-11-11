@echo off
title Universal Clipboard for Windows (iPhone -> Windows)
cd /d "%~dp0"
echo Starting Universal Clipboard for Windows (Auto USB / Wi-Fi mode)...
python -u poc_universal_clipboard.py --transport auto --sync-on-connect
pause
