FROM python:3.12-slim

# Keine Buffer-Verzögerung bei Logs, keine .pyc-Dateien im Image
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistenter Speicherort für Nachrichten-IDs/Preisstand (siehe fly.toml -> [mounts])
RUN mkdir -p /app/data
VOLUME ["/app/data"]

CMD ["python", "bot.py"]
