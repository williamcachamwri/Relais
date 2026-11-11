@echo off
title Universal Clipboard for Windows
cd /d "%~dp0"
if exist "bin\x64\Release\net10.0-windows10.0.19041.0\win-x64\UniversalClipboard.exe" (
    start "" "bin\x64\Release\net10.0-windows10.0.19041.0\win-x64\UniversalClipboard.exe"
) else (
    python -u universalClipboard.py --transport auto --sync-on-connect
)
