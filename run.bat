@echo off
rem Halfword: запуск в фоне, иконка в трее. Первый раз — install.bat.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -X utf8 -m halfword run
) else (
    start "" pyw -3 -X utf8 -m halfword run
)
