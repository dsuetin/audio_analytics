FROM python:3.12-slim

WORKDIR /app

# system deps (если будет grpc / aio / psycopg)
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Доверенные CA-сертификаты (MAX / Минцифра).
# MAX выступает с сертификатом, выпущенным Минцифрой (Russian Trusted Sub CA),
# которого нет в стандартном бандле Debian. Без него curl/опыты из контейнера
# падают на TLS-верификации platform-api2.max.ru.
# Python-код (alert_service/max_bot.py) уже грузит этот CA из alert_service/certs/max_ca.pem
# в собственный SSLContext (с X509_V_FLAG_PARTIAL_CHAIN), так что сам
# alert_service работает без этих ENV-переменных. Но они полезны для
# curl/health-проверок и любого другого инструмента, который не знает
# про Python-SSLContext.
COPY alert_service/certs/ /usr/local/share/ca-certificates/max/
RUN update-ca-certificates

COPY alert_service/requirements.txt .
# RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt

# RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
# COPY . .
# RUN pip install requests==2.34.2
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "alert_service.alerts_service"]