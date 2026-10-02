@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo === Установка Halfword ===
echo.

where py >nul 2>nul || goto :no_python
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" || goto :no_python

if not exist ".venv\Scripts\python.exe" (
    echo Создаю окружение Python...
    py -3 -m venv .venv || goto :fail
)
echo Ставлю зависимости...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail

echo.
choice /c YN /m "Скачать локальную ИИ-модель (около 0.8 ГБ, нужна для подсказок-продолжений)"
if errorlevel 2 goto :shortcut
".venv\Scripts\python.exe" -X utf8 -m halfword setup || echo Не получилось. Повторить можно позже: трей, "Установить ИИ-модель..."

:shortcut
call :make_link "[Environment]::GetFolderPath('Desktop')"
echo.
choice /c YN /m "Запускать Halfword при входе в Windows"
if errorlevel 2 goto :start
call :make_link "[Environment]::GetFolderPath('Startup')"

:start
start "" ".venv\Scripts\pythonw.exe" -X utf8 -m halfword run
echo.
echo Готово: Halfword работает, иконка в трее, ярлык на рабочем столе.
pause
exit /b 0

:make_link
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%~1+'\Halfword.lnk'); $s.TargetPath='%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments='-X utf8 -m halfword run'; $s.WorkingDirectory='%~dp0'; $s.Description='Halfword: подсказки при наборе'; $s.Save()"
exit /b 0

:no_python
echo Нужен Python 3.10 или новее: https://www.python.org/downloads/
echo При установке оставьте галочку "py launcher".
pause
exit /b 1

:fail
echo Установка не удалась, сообщение об ошибке выше.
pause
exit /b 1
