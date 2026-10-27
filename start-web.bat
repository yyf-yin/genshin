@echo off
rem One-click launcher for the web Q&A. First run creates .venv and installs dependencies.
rem DeepSeek key: ds_key.txt in this folder or its parent, or env var DEEPSEEK_API_KEY.
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] creating .venv ...
    py -3.13 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 goto :fail
    ".venv\Scripts\python.exe" -m pip install -q -e .
    if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" -c "import genshin_assistant.chat" 2>nul || ".venv\Scripts\python.exe" -m pip install -q -e .

echo Starting http://127.0.0.1:8765/  (close this window to stop)
".venv\Scripts\python.exe" -m genshin_assistant web %*
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo Failed. See the error above.
pause
exit /b 1
