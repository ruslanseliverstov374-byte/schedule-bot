# -*- coding: utf-8 -*-
"""Автозапуск бота расписания на этом компьютере (планировщик Windows).

Создаёт две задачи:
  1) ScheduleBot         — запуск бота при входе в систему, без окна консоли;
  2) ScheduleBotWatchdog — проверка каждые 10 минут: если бот упал, поднять.

Запуск:   python tools/install_autostart.py
Удаление: python tools/install_autostart.py --remove
"""

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASK_BOT = "ScheduleBot"
TASK_WATCH = "ScheduleBotWatchdog"


def pythonw():
    executable = sys.executable
    if os.name == "nt":
        candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return executable


def run_powershell(script):
    """Выполняет PowerShell-скрипт из временного файла (без проблем с кавычками)."""
    path = os.path.join(ROOT, "data", "_autostart.ps1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig") as handle:
        handle.write(script)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return result


def install():
    executable = pythonw()
    script = """
$ErrorActionPreference = 'Stop'
$exe = '{exe}'
$root = '{root}'

$botAction = New-ScheduledTaskAction -Execute $exe -Argument 'bot.py' -WorkingDirectory $root
$botTrigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName '{task_bot}' -Action $botAction -Trigger $botTrigger -Settings $settings -Description 'Бот расписания Поволжского ГУФКСиТ: запуск при входе' -Force | Out-Null

$watchAction = New-ScheduledTaskAction -Execute $exe -Argument 'tools\\watchdog.py' -WorkingDirectory $root
$watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date
$watchTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration ([TimeSpan]::MaxValue)).Repetition
Register-ScheduledTask -TaskName '{task_watch}' -Action $watchAction -Trigger $watchTrigger -Settings $settings -Description 'Бот расписания: сторож каждые 10 минут' -Force | Out-Null

Start-ScheduledTask -TaskName '{task_bot}'
Write-Output 'OK'
""".format(exe=executable.replace("'", "''"), root=ROOT.replace("'", "''"),
           task_bot=TASK_BOT, task_watch=TASK_WATCH)
    result = run_powershell(script)
    if result.returncode != 0 or "OK" not in (result.stdout or ""):
        print("[!] Не удалось создать задачи планировщика.")
        print(result.stdout or "")
        print(result.stderr or "")
        return 1
    print("[+] Созданы задачи планировщика:")
    print("    %s         — запуск бота при входе в систему" % TASK_BOT)
    print("    %s — проверка каждые 10 минут" % TASK_WATCH)
    print("[+] Бот уже запущен. Окно консоли не появляется.")
    print("    Логи: logs\\bot.log и logs\\watchdog.log")
    print("    Убрать автозапуск: remove_autostart.bat")
    return 0


def remove():
    script = """
$ErrorActionPreference = 'SilentlyContinue'
foreach ($name in @('{task_bot}', '{task_watch}')) {{
    Stop-ScheduledTask -TaskName $name
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}}
$lock = '{lock}'
$pidFile = '{pid}'
if (Test-Path $pidFile) {{
    $botPid = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($botPid) {{ Stop-Process -Id ([int]$botPid) -Force -ErrorAction SilentlyContinue }}
}}
Write-Output 'OK'
""".format(task_bot=TASK_BOT, task_watch=TASK_WATCH,
           lock=os.path.join(ROOT, "data", "bot.lock").replace("'", "''"),
           pid=os.path.join(ROOT, "data", "bot.lock.pid").replace("'", "''"))
    result = run_powershell(script)
    if result.returncode == 0:
        print("[+] Автозапуск убран, задачи удалены, бот остановлен.")
        return 0
    print("[!] Не удалось удалить задачи:")
    print(result.stderr or result.stdout or "")
    return 1


def main():
    parser = argparse.ArgumentParser(description="Автозапуск бота расписания")
    parser.add_argument("--remove", action="store_true", help="убрать автозапуск")
    arguments = parser.parse_args()
    if os.name != "nt":
        print("Автозапуск через планировщик Windows доступен только на Windows.")
        print("На Linux используйте deploy/install_vps.sh (systemd).")
        return 1
    return remove() if arguments.remove else install()


if __name__ == "__main__":
    sys.exit(main())
