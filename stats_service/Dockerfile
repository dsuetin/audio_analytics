FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    fonts-dejavu-core fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

COPY stats_service/requirements.txt /app/stats_service/requirements.txt
RUN pip install --no-cache-dir -r /app/stats_service/requirements.txt

COPY scripts /app/scripts
COPY stats_service /app/stats_service

CMD ["python3", "-m", "stats_service.scheduler"]
