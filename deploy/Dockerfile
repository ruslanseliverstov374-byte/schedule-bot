# Бот расписания Поволжского ГУФКСиТ — в Docker.
#
# Сборка и запуск:
#   docker build -t schedule-bot -f deploy/Dockerfile .
#   docker run -d --name schedule-bot --restart unless-stopped \
#       -e BOT_TOKEN=8123456789:AAH... -v schedule-bot-data:/app/data schedule-bot
#
# База живёт в томе schedule-bot-data, поэтому переживает пересборку образа.

FROM python:3.12-slim

WORKDIR /app

COPY . /app/
RUN rm -rf /app/data /app/logs /app/token.txt /app/.env && mkdir -p /app/data /app/logs

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV REFRESH_MINUTES=20

VOLUME ["/app/data"]

# Проверка живости: бот каждые 20 минут обращается к API расписания,
# а этот тест подтверждает, что код и база на месте.
HEALTHCHECK --interval=5m --timeout=60s --start-period=30s \
    CMD python /app/bot.py --check --db /tmp/healthcheck.db || exit 1

CMD ["python", "bot.py"]
