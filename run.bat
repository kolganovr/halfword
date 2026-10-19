@echo off
rem Halfword: запуск в фоне, иконка в трее. Первый раз — install.bat.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -X utf8 -m halfword run
    exit /b 0
)
rem Без .venv берём первый установленный Python, где есть зависимости: самый новый (pyw -3) может быть без них.
for /f "tokens=1" %%v in ('py -0 2^>nul') do (
    py %%v -c "import comtypes, pystray, PIL" >nul 2>nul && (
        start "" pyw %%v -X utf8 -m halfword run
        exit /b 0
    )
)
start "" pyw -3 -X utf8 -m halfword run
