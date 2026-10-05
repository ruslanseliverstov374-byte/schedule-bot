@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist token.txt (
    if "%BOT_TOKEN%"=="" (
        echo [!] Токен не найден. Запустите setup.bat - он спросит токен и всё настроит.
        pause
        exit /b 1
    )
)

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python не найден в PATH. Установите Python 3.9+ с https://python.org
    pause
    exit /b 1
)

:loop
python bot.py
echo.
echo Бот остановился. Перезапуск через 5 секунд. Закрыть окно - Ctrl+C.
timeout /t 5 >nul
goto loop
