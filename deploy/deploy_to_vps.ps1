<#
.SYNOPSIS
    Переносит бота расписания на VPS и запускает его круглосуточно.

.DESCRIPTION
    Запускается на вашем компьютере. Делает всё сам:
      1) собирает архив проекта (без базы, логов и токена);
      2) копирует его на сервер по SCP;
      3) запускает установку: systemd-служба, автозапуск, перезапуск при сбоях;
      4) переносит базу (если она есть), чтобы группы, ДЗ и настройки сохранились;
      5) показывает статус и подсказывает дальнейшие шаги.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\deploy_to_vps.ps1 -Server 203.0.113.10

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\deploy_to_vps.ps1 -Server 203.0.113.10 -User ubuntu -Port 2222
#>

param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$User = "root",
    [int]$Port = 22,
    [string]$Token,
    [string]$RemoteDir = "/opt/schedule-bot",
    [string]$KeyFile = "",
    [switch]$NoDatabase
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $projectDir
Write-Host "==> Проект: $projectDir" -ForegroundColor Cyan

if (-not $Token) {
    $tokenFile = Join-Path $projectDir "token.txt"
    if (Test-Path $tokenFile) { $Token = (Get-Content $tokenFile -Raw).Trim() }
}
if (-not $Token) {
    throw "Не найден токен: положите его в token.txt или передайте -Token 8123456789:AAH..."
}
Write-Host ("==> Токен: {0}...{1}" -f $Token.Substring(0, [Math]::Min(10, $Token.Length)), $Token.Substring([Math]::Max(0, $Token.Length - 4)))

foreach ($tool in @("ssh", "scp", "tar")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Не найден $tool. Включите компонент Windows «Клиент OpenSSH» (Параметры → Приложения → Дополнительные компоненты)."
    }
}

# --- 1. Архив проекта ------------------------------------------------------
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archive = Join-Path $env:TEMP "schedule-bot-$stamp.tar.gz"
$staging = Join-Path $env:TEMP "schedule-bot-pack-$stamp"
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging | Out-Null

Write-Host "==> Собираю архив (без базы, логов и токена)" -ForegroundColor Cyan
$excludeDirs = @("data", "logs", "__pycache__", ".git")
$excludeFiles = @("token.txt", ".env")
Get-ChildItem -Path $projectDir -Force | Where-Object {
    $_.Name -notin $excludeDirs -and $_.Name -notin $excludeFiles
} | ForEach-Object {
    Copy-Item $_.FullName -Destination $staging -Recurse -Force
}
Get-ChildItem -Path $staging -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Push-Location $staging
try {
    & tar -czf $archive .
    if ($LASTEXITCODE -ne 0) { throw "tar завершился с ошибкой" }
} finally { Pop-Location }
Write-Host ("==> Архив: {0} ({1:N0} КБ)" -f $archive, ((Get-Item $archive).Length / 1KB))

# --- 2. Копирование на сервер ---------------------------------------------
$sshArgs = @("-p", "$Port", "-o", "StrictHostKeyChecking=accept-new")
$scpArgs = @("-P", "$Port", "-o", "StrictHostKeyChecking=accept-new")
if ($KeyFile) {
    $sshArgs += @("-i", $KeyFile)
    $scpArgs += @("-i", $KeyFile)
}
$target = "$User@$Server"
$remoteArchive = "/tmp/schedule-bot-$stamp.tar.gz"

Write-Host "==> Копирую архив на $target" -ForegroundColor Cyan
& scp @scpArgs $archive "${target}:$remoteArchive"
if ($LASTEXITCODE -ne 0) { throw "scp не смог скопировать архив (проверьте IP, пользователя и ключ)" }
& scp @scpArgs (Join-Path $staging "deploy/install_vps.sh") "${target}:/tmp/install_vps.sh"
if ($LASTEXITCODE -ne 0) { throw "scp не смог скопировать установщик" }

# --- 3. Установка на сервере ----------------------------------------------
Write-Host "==> Устанавливаю службу на сервере" -ForegroundColor Cyan
$installCmd = "bash /tmp/install_vps.sh --archive $remoteArchive --dir '$RemoteDir' --token '$Token'"
& ssh @sshArgs $target $installCmd
if ($LASTEXITCODE -ne 0) { throw "Установка на сервере завершилась с ошибкой" }

# --- 4. Перенос базы -------------------------------------------------------
if (-not $NoDatabase) {
    $liveDb = Join-Path $projectDir "data\bot.db"
    if (Test-Path $liveDb) {
        Write-Host "==> Переношу базу (группы, ДЗ, настройки)" -ForegroundColor Cyan
        $dbArchive = Join-Path $env:TEMP "schedule-bot-db-$stamp.tar.gz"
        Push-Location (Join-Path $projectDir "data")
        try { & tar -czf $dbArchive "bot.db" } finally { Pop-Location }
        & scp @scpArgs $dbArchive "${target}:/tmp/schedule-bot-db.tar.gz"
        & ssh @sshArgs $target "mkdir -p '$RemoteDir/data' && tar -xzf /tmp/schedule-bot-db.tar.gz -C '$RemoteDir/data' && systemctl restart schedule-bot && echo '==> База перенесена, служба перезапущена'"
        Remove-Item $dbArchive -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "==> Локальной базы нет — начнём с чистой" -ForegroundColor Yellow
    }
}

Remove-Item $archive -Force -ErrorAction SilentlyContinue
Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue

# --- 5. Итог ---------------------------------------------------------------
Write-Host ""
Write-Host "==> Проверяю работу службы" -ForegroundColor Cyan
& ssh @sshArgs $target "systemctl is-active schedule-bot; tail -n 8 '$RemoteDir/logs/bot.log' 2>/dev/null || true"

Write-Host ""
Write-Host "ГОТОВО. Бот работает круглосуточно на сервере." -ForegroundColor Green
Write-Host "Напишите боту в Telegram /start — и выберите свою группу."
Write-Host ""
Write-Host "Полезные команды:"
Write-Host "  ssh $target `"systemctl status schedule-bot`""
Write-Host "  ssh $target `"systemctl restart schedule-bot`""
Write-Host "  ssh $target `"tail -f $RemoteDir/logs/bot.log`""
Write-Host ""
Write-Host "Если бот запущен и на этом компьютере — выполните remove_autostart.bat," -ForegroundColor Yellow
Write-Host "иначе два экземпляра будут мешать друг другу (Telegram отдаёт ошибку Conflict)." -ForegroundColor Yellow
