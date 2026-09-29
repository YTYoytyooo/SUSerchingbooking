@echo off
chcp 65001 >nul
powershell.exe -NoProfile -File "%~dp0scripts\launcher.ps1" -Command install
