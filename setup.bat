@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   БОТ РАСПИСАНИЯ Поволжского ГУФКСиТ - установка за минуту
echo ============================================================
echo.
where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python не найден в PATH.
    echo     Установите Python 3.9+ с https://python.org и запустите файл снова.
    echo     При установке отметьте галочку "Add python.exe to PATH".
    pause
    exit /b 1
)

if exist token.txt (
    echo Найден сохранённый токен в token.txt
    set /p REPLACE=Заменить его? (y/N): 
    if /i not "!REPLACE!"=="y" goto :run
)

echo.
echo Шаг 1. Откройте Telegram и найдите бота @BotFather
echo Шаг 2. Отправьте ему команду /newbot
echo Шаг 3. Придумайте имя бота (например, Raspisanie PGUFKSiT)
echo Шаг 4. Скопируйте токен вида 8123456789:AAH... и вставьте ниже
echo.
set /p TOKEN=Токен бота: 
if "!TOKEN!"=="" (
    echo [!] Токен не введён - выходим.
    pause
    exit /b 1
)
(echo !TOKEN!)>token.txt
echo.
echo [+] Токен сохранён в token.txt

:run
echo.
echo Проверяю связь с сайтом расписания и базу...
python bot.py --check
if errorlevel 1 (
    echo.
    echo [!] Проверка не прошла. Скорее всего, нет интернета или сайт расписания
    echo     недоступен. Бот всё равно запустится и будет работать с кэшем.
    echo.
)

echo.
echo Запускаю бота. Не закрывайте это окно, пока хотите, чтобы бот работал.
echo Остановить бота: Ctrl+C
echo Чтобы бот работал круглосуточно без этого окна:
echo   1) install_autostart.bat - на этом компьютере;
echo   2) deploy\deploy_to_vps.ps1 -Server IP - на сервере (работает всегда).
echo.
python bot.py
echo.
echo Бот остановлен.
pause
