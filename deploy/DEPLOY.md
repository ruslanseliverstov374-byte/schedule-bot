# Установка бота на VPS (круглосуточно)

Бот работает всегда — даже когда ваш компьютер выключен: расписание проверяется
на сервере, напоминания приходят вовремя.

---

## 1. Что нужно

| Что | Сколько стоит | Где взять |
|---|---|---|
| VPS с Ubuntu/Debian, 1 ядро, 512 МБ–1 ГБ | 150–300 ₽/мес | Timeweb Cloud, FirstVDS, VDSina, Aeza, Reg.ru |
| IP-адрес и пароль root | — | в панели хостинга после оплаты |

Бот очень лёгкий: около 40 МБ памяти, база — сотни килобайт. Подойдёт самый дешёвый тариф.
Если у вас уже есть VPS с другим ботом — этот спокойно встанет рядом.

---

## 2. Установка одной командой

На **своём компьютере**, в папке проекта:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\deploy_to_vps.ps1 -Server 203.0.113.10
```

Если вход не под root или порт другой:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\deploy_to_vps.ps1 -Server 203.0.113.10 -User ubuntu -Port 2222
```

Токен скрипт возьмёт из `token.txt` (или передайте `-Token 8123456789:AAH...`).

Что произойдёт:

1. соберётся архив проекта (без базы, логов и токена — секреты не уезжают в архив);
2. архив и установщик скопируются на сервер по SCP;
3. на сервере создастся `/opt/schedule-bot`, установится systemd-служба `schedule-bot`
   с автозапуском и перезапуском при сбоях;
4. если у вас уже есть рабочая база `data/bot.db` — она перенесётся, группы, ДЗ и настройки
   сохранятся;
5. скрипт покажет статус службы и последние строки лога.

Нужен только компонент Windows «Клиент OpenSSH» (Параметры → Приложения →
Дополнительные компоненты). Пароль сервера спросят один раз.

**После установки запустите `remove_autostart.bat`**, если бот был настроен на автозапуск
на вашем ПК: Telegram не разрешает двум копиям бота работать одновременно (в логе будет
`Conflict: terminated by other getUpdates request`).

---

## 3. Проверка и управление

```bash
ssh root@IP "systemctl status schedule-bot"      # состояние службы
ssh root@IP "systemctl restart schedule-bot"     # перезапуск
ssh root@IP "tail -n 50 /opt/schedule-bot/logs/bot.log"   # логи
ssh root@IP "python3 /opt/schedule-bot/bot.py --check"    # проверка API и базы
```

Остановить и убрать автозапуск:

```bash
ssh root@IP "systemctl disable --now schedule-bot"
```

---

## 4. Если ставите вручную

```bash
# на сервере
mkdir -p /opt/schedule-bot
cd /opt/schedule-bot
# залейте туда файлы проекта, например с компьютера:
#   scp -r schedule-bot/* root@IP:/opt/schedule-bot/

echo "BOT_TOKEN=8123456789:AAH..." > /opt/schedule-bot/.env
chmod 600 /opt/schedule-bot/.env

bash /opt/schedule-bot/deploy/install_vps.sh --dir /opt/schedule-bot
```

Или совсем без скрипта:

```bash
sed -e "s|__DIR__|/opt/schedule-bot|g" -e "s|__USER__|root|g" \
    /opt/schedule-bot/deploy/schedule-bot.service > /etc/systemd/system/schedule-bot.service
systemctl daemon-reload && systemctl enable --now schedule-bot
```

---

## 5. Docker (альтернатива)

```bash
docker build -t schedule-bot -f deploy/Dockerfile .
docker run -d --name schedule-bot --restart unless-stopped \
    -e BOT_TOKEN=8123456789:AAH... \
    -v schedule-bot-data:/app/data \
    schedule-bot
```

База живёт в томе `schedule-bot-data` и переживает пересборку образа. У образа есть
healthcheck: он проверяет, что API расписания отвечает и база открывается.

---

## 6. Обновление бота

Запустите тот же скрипт ещё раз — он перезапишет код, но **не тронет** `.env` (токен) и
`data/bot.db` (группы, ДЗ, настройки):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\deploy_to_vps.ps1 -Server 203.0.113.10 -NoDatabase
```

Флаг `-NoDatabase` означает «не переносить базу с компьютера» — на сервере останется её
текущая версия. Без флага локальная база заменит серверную (полезно при первом переносе).

---

## 7. Если что-то не так

| Симптом | Причина и решение |
|---|---|
| `Conflict: terminated by other getUpdates request` | Бот запущен в двух местах. Остановите один: `remove_autostart.bat` на ПК или `systemctl stop schedule-bot` на VPS |
| `401 Unauthorized` в логе | Токен неверный или отозван. Проверьте `BOT_TOKEN` в `/opt/schedule-bot/.env` и перезапустите службу |
| `API вернул не JSON` / `HTTP 404 от API` | Вуз сменил ключ или адрес API. Инструкция по восстановлению — [../docs/API.md](../docs/API.md), раздел «Если API изменился» |
| Служба перезапускается по кругу | Смотрите `logs/bot.log`: обычно это неверный токен или отсутствие интернета |
| `scp: command not found` на своём ПК | Включите «Клиент OpenSSH» в дополнительных компонентах Windows |
| Напоминания приходят не вовремя | Проверьте часовой пояс на сервере: бот считает время как UTC+3 (Казань) независимо от настроек сервера, но системные часы должны быть верными (`timedatectl`) |
