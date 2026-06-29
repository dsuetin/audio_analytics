FROM python:3.12-slim

WORKDIR /app

COPY classification/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY classification /app/classification

ENV PYTHONPATH=/app

CMD ["python", "-m", "classification.service"]