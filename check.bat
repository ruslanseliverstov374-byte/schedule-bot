@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   ПРОВЕРКА БОТОВ РАСПИСАНИЯ (ПГУФКСиТ и КГАСУ)
echo ============================================================
echo.
echo [1/12] Связь с сайтом расписания, база, разбор дат и ДЗ
python bot.py --check
if errorlevel 1 (
    echo.
    echo [!] Проверка не прошла - смотрите сообщения выше.
    pause
    exit /b 1
)

echo.
echo [2/12] Сценарии бота на офлайн-моке API
python tests\simulate.py
if errorlevel 1 (
    echo.
    echo [!] Сценарии бота не прошли.
    pause
    exit /b 1
)

echo.
echo [3/12] Самопроверка офлайн-мока API
python tests\test_mock_api.py
if errorlevel 1 (
    echo.
    echo [!] Мок API не прошёл самопроверку.
    pause
    exit /b 1
)

echo.
echo [4/12] Health-страница и приём вебхуков Telegram
python tests\test_webapp.py
if errorlevel 1 (
    echo.
    echo [!] Веб-слой не прошёл проверки.
    pause
    exit /b 1
)

echo.
echo [5/12] Копии базы: сохранение и восстановление
python tests\test_snapshot.py
if errorlevel 1 (
    echo.
    echo [!] Копии базы не прошли проверки.
    pause
    exit /b 1
)

echo.
echo [6/12] Парсер расписаний КГАСУ в формате .docx
python tests\test_docxparse.py
if errorlevel 1 (
    echo.
    echo [!] Разбор .docx не прошёл проверки.
    pause
    exit /b 1
)

echo.
echo [7/12] Парсер расписаний КГАСУ в старом формате .doc
python tests\test_docparse.py
if errorlevel 1 (
    echo.
    echo [!] Разбор .doc не прошёл проверки.
    pause
    exit /b 1
)

echo.
echo [8/12] Источник расписания КГАСУ: группы, подгруппы, чётные недели
python tests\test_kgasu.py
if errorlevel 1 (
    echo.
    echo [!] Провайдер КГАСУ не прошёл проверки.
    pause
    exit /b 1
)

echo.
echo [9/12] Разбор PDF-сканов КГАСУ (распознавание через tesseract)
python tests\test_pdfparse.py
if errorlevel 1 (
    echo.
    echo [!] Разбор PDF-сканов не прошёл проверки.
    pause
    exit /b 1
)

echo.
echo [10/12] Сводки и выгрузки по пользователям
python tests\test_report.py
if errorlevel 1 (
    echo.
    echo [!] Отчёты не прошли проверки.
    pause
    exit /b 1
)

echo.
echo [11/12] Админ-функции: рассылка и данные по пользователям
python tests\test_admin.py
if errorlevel 1 (
    echo.
    echo [!] Админ-функции не прошли проверки.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo.
echo [12/12] Встроенная Планёрка: вход, API, задачи
python tests\\test_planner_cloud.py
if errorlevel 1 (
    echo.
    echo [!] Планёрка не прошла проверки.
    pause
    exit /b 1
)

echo   ВСЁ В ПОРЯДКЕ: боты готовы к работе
echo ============================================================
pause
