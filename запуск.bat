@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto system_python
".venv\Scripts\python.exe" -c "import flask, werkzeug, jinja2, click, itsdangerous, blinker" >nul 2>&1
if errorlevel 1 goto incomplete_venv
set "TRACKER_PYTHON=%~dp0.venv\Scripts\python.exe"
goto run
:incomplete_venv
echo В .venv отсутствуют зависимости. Проверяю системный Python...
:system_python
python -c "import flask, werkzeug, jinja2, click, itsdangerous, blinker" >nul 2>&1
if errorlevel 1 goto py_launcher
set "TRACKER_PYTHON=python"
goto run
:py_launcher
py -3 -c "import flask, werkzeug, jinja2, click, itsdangerous, blinker" >nul 2>&1
if errorlevel 1 goto missing
py -3 launcher.py
if errorlevel 1 goto fail
exit /b 0
:run
"%TRACKER_PYTHON%" launcher.py
if errorlevel 1 goto fail
exit /b 0
:missing
echo Не найден Python с зависимостями Manager Tracker.
echo Автоматическое скачивание при запуске отключено.
echo Для отдельной установки откройте PowerShell в папке проекта:
echo   python -m venv .venv
echo   .\.venv\Scripts\python.exe -m pip install --timeout 15 --retries 0 -r requirements.txt
echo Затем повторно запустите этот BAT-файл.
:fail
echo.
echo Приложение не запущено. Причина указана выше. Окно оставлено открытым.
pause
exit /b 1
