@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   УБРАТЬ АВТОЗАПУСК БОТА
echo ============================================================
echo.
echo Будут удалены задачи планировщика ScheduleBot и ScheduleBotWatchdog,
echo а запущенный бот остановлен. База и настройки сохранятся.
echo.
set /p CONFIRM=Продолжить? (y/N): 
if /i not "%CONFIRM%"=="y" (
    echo Отменено.
    pause
    exit /b 0
)

python tools\install_autostart.py --remove
echo.
pause
