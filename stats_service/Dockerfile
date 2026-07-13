FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY stats_service/requirements.txt /app/stats_service/requirements.txt
RUN pip install --no-cache-dir -r /app/stats_service/requirements.txt

COPY scripts /app/scripts
COPY stats_service /app/stats_service

CMD ["python3", "-m", "stats_service.scheduler"]
