@echo off
cd /d "%~dp0"

call venv\Scripts\activate.bat

python bot.py

echo.
echo ===========================
echo Бот остановлен.
pause