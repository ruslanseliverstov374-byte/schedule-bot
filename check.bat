@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   ПРОВЕРКА БОТОВ РАСПИСАНИЯ (ПГУФКСиТ и КГАСУ)
echo ============================================================
echo.
echo [1/10] Связь с сайтом расписания, база, разбор дат и ДЗ
python bot.py --check
if errorlevel 1 (
    echo.
    echo [!] Проверка не прошла - смотрите сообщения выше.
    pause
    exit /b 1
)

echo.
echo [2/10] Сценарии бота на офлайн-моке API
python tests\simulate.py
if errorlevel 1 (
    echo.
    echo [!] Сценарии бота не прошли.
    pause
    exit /b 1
)

echo.
echo [3/10] Самопроверка офлайн-мока API
python tests\test_mock_api.py
if errorlevel 1 (
    echo.
    echo [!] Мок API не прошёл самопроверку.
    pause
    exit /b 1
)

echo.
echo [4/10] Health-страница и приём вебхуков Telegram
python tests\test_webapp.py
if errorlevel 1 (
    echo.
    echo [!] Веб-слой не прошёл проверки.
    pause
    exit /b 1
)

echo.
echo [5/10] Копии базы: сохранение и восстановление
python tests\test_snapshot.py
if errorlevel 1 (
    echo.
    echo [!] Копии базы не прошли проверки.
    pause
    exit /b 1
)

echo.
echo [6/10] Парсер расписаний КГАСУ в формате .docx
python tests\test_docxparse.py
if errorlevel 1 (
    echo.
    echo [!] Разбор .docx не прошёл проверки.
    pause
    exit /b 1
)

echo.
echo [7/10] Парсер расписаний КГАСУ в старом формате .doc
python tests\test_docparse.py
if errorlevel 1 (
    echo.
    echo [!] Разбор .doc не прошёл проверки.
    pause
    exit /b 1
)

echo.
echo [8/10] Источник расписания КГАСУ: группы, подгруппы, чётные недели
python tests\test_kgasu.py
if errorlevel 1 (
    echo.
    echo [!] Провайдер КГАСУ не прошёл проверки.
    pause
    exit /b 1
)

echo.
echo [9/10] Сводки и выгрузки по пользователям
python tests\test_report.py
if errorlevel 1 (
    echo.
    echo [!] Отчёты не прошли проверки.
    pause
    exit /b 1
)

echo.
echo [10/10] Админ-функции: рассылка и данные по пользователям
python tests\test_admin.py
if errorlevel 1 (
    echo.
    echo [!] Админ-функции не прошли проверки.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ВСЁ В ПОРЯДКЕ: боты готовы к работе
echo ============================================================
pause
