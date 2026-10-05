@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   ПРОВЕРКА БОТА РАСПИСАНИЯ
echo ============================================================
echo.
echo [1/5] Связь с сайтом расписания, база, разбор дат и ДЗ
python bot.py --check
if errorlevel 1 (
    echo.
    echo [!] Проверка не прошла - смотрите сообщения выше.
    pause
    exit /b 1
)

echo.
echo [2/5] Сценарии бота на офлайн-моке API
python tests\simulate.py
if errorlevel 1 (
    echo.
    echo [!] Сценарии бота не прошли.
    pause
    exit /b 1
)

echo.
echo [3/5] Самопроверка офлайн-мока API
python tests\test_mock_api.py
if errorlevel 1 (
    echo.
    echo [!] Мок API не прошёл самопроверку.
    pause
    exit /b 1
)

echo.
echo [4/5] Health-страница и приём вебхука Telegram
python tests\test_webapp.py
if errorlevel 1 (
    echo.
    echo [!] Веб-слой не прошёл проверки.
    pause
    exit /b 1
)

echo.
echo [5/5] Копии базы: сохранение и восстановление
python tests\test_snapshot.py
if errorlevel 1 (
    echo.
    echo [!] Копии базы не прошли проверки.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ВСЁ В ПОРЯДКЕ: бот готов к работе
echo ============================================================
pause
