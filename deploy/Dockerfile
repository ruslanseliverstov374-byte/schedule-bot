# Боты расписания (ПГУФКСиТ и КГАСУ) — в Docker.
#
# Сборка и запуск одного бота:
#   docker build -t schedule-bot -f deploy/Dockerfile .
#   docker run -d --name schedule-bot --restart unless-stopped \
#       -e BOT_TOKEN=8123456789:AAH... -v schedule-bot-data:/app/data schedule-bot
#
# Два бота в одном контейнере (так дешевле на бесплатном тарифе Render):
#   -e BOT_TOKEN=... -e BOT_TOKEN_KGASU=... -e OWNER_ID=1368878379
#
# База живёт в томе schedule-bot-data, поэтому переживает пересборку образа.

FROM python:3.12-slim

WORKDIR /app

COPY . /app/
RUN rm -rf /app/data /app/logs /app/token.txt /app/token-kgasu.txt /app/.env \
    && mkdir -p /app/data /app/logs

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV REFRESH_MINUTES=20

VOLUME ["/app/data"]

# Проверка живости: бот каждые 20 минут обращается к источнику расписания,
# а этот тест подтверждает, что код и база на месте.
HEALTHCHECK --interval=5m --timeout=60s --start-period=30s \
    CMD python /app/bot.py --check --db /tmp/healthcheck.db || exit 1

# start.py сам решает: один бот или два (если задан BOT_TOKEN_KGASU).
CMD ["python", "start.py"]
