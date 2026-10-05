#!/usr/bin/env bash
# Установка бота расписания на VPS (Ubuntu/Debian): распаковка, systemd, автозапуск.
#
# Обычно запускается автоматически с вашего компьютера:
#   powershell -ExecutionPolicy Bypass -File deploy\deploy_to_vps.ps1 -Server <IP>
#
# Вручную на сервере:
#   bash install_vps.sh --archive /tmp/schedule-bot.tar.gz --dir /opt/schedule-bot \
#        --token 8123456789:AAH...
set -euo pipefail

ARCHIVE=""
TARGET_DIR="/opt/schedule-bot"
TOKEN=""
SERVICE_NAME="schedule-bot"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --archive) ARCHIVE="$2"; shift 2 ;;
        --dir) TARGET_DIR="$2"; shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --service) SERVICE_NAME="$2"; shift 2 ;;
        *) echo "Неизвестный параметр: $1"; exit 1 ;;
    esac
done

echo "==> Папка установки: $TARGET_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "==> Устанавливаю python3..."
    apt-get update -qq
    apt-get install -y -qq python3
fi
echo "==> Python: $(python3 --version)"

mkdir -p "$TARGET_DIR"

if [[ -n "$ARCHIVE" ]]; then
    echo "==> Распаковываю $ARCHIVE"
    tar -xzf "$ARCHIVE" -C "$TARGET_DIR"
fi

cd "$TARGET_DIR"
mkdir -p logs data

# База и токен не должны потеряться при обновлении: .env только дополняем
if [[ -n "$TOKEN" ]]; then
    if [[ -f .env ]] && grep -q '^BOT_TOKEN=' .env; then
        sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$TOKEN|" .env
    else
        echo "BOT_TOKEN=$TOKEN" >> .env
    fi
    chmod 600 .env
    echo "==> Токен записан в $TARGET_DIR/.env"
fi

if [[ ! -f .env ]] || ! grep -q '^BOT_TOKEN=' .env; then
    echo "!! В $TARGET_DIR/.env нет BOT_TOKEN — бот не сможет подключиться к Telegram."
    echo "   Добавьте строку BOT_TOKEN=8123456789:AAH... и перезапустите службу."
fi

# Непривилегированный пользователь для службы
RUN_USER="root"
if id -u botuser >/dev/null 2>&1; then
    RUN_USER="botuser"
fi
chown -R "$RUN_USER":"$RUN_USER" "$TARGET_DIR" 2>/dev/null || true

echo "==> Ставлю systemd-службу $SERVICE_NAME (пользователь $RUN_USER)"
sed -e "s|__DIR__|$TARGET_DIR|g" -e "s|__USER__|$RUN_USER|g" \
    "$TARGET_DIR/deploy/schedule-bot.service" > "/etc/systemd/system/$SERVICE_NAME.service"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"
sleep 3

echo
echo "==> Статус службы:"
systemctl --no-pager --full status "$SERVICE_NAME" | head -n 15 || true
echo
echo "==> Последние строки лога:"
tail -n 15 "$TARGET_DIR/logs/bot.log" 2>/dev/null || echo "(лог пока пуст)"
echo
echo "==> Готово. Полезные команды:"
echo "    systemctl status $SERVICE_NAME"
echo "    systemctl restart $SERVICE_NAME"
echo "    tail -f $TARGET_DIR/logs/bot.log"
