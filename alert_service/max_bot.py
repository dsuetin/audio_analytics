import os
import ssl
from pathlib import Path

import requests


# Официальный API MAX: https://dev.max.ru/docs-api
# - base_url: platform-api2.max.ru (не platform-api — см. предупреждение в документации)
# - токен передаётся в заголовке Authorization, НЕ в query-параметрах
# - POST /messages?chat_id=<int> — отправка в чат/канал
# - POST /messages?user_id=<int> — отправка пользователю
# - limit: <= 2 сообщения/сек в один чат, текст <= 4000 символов
# - коды: 200 ok / 401 невалидный токен / 500 внутренняя ошибка
#
# TLS: MAX выступает с сертификатом, выпущенным Минцифрой
# (Russian Trusted Sub CA), которого нет в стандартных CA-бандлах.
# Документация MAX требует добавить этот CA в доверенные.
# Мы шлём его вместе с пакетом (alert_service/certs/max_ca.pem) и грузим
# в собственный SSLContext, разрешая якорение по non-self-issued CA
# (X509_V_FLAG_PARTIAL_CHAIN).
_PACKAGE_CERTS = Path(__file__).resolve().parent / "certs"
_DEFAULT_CA = _PACKAGE_CERTS / "max_ca.pem"


def _allow_partial_chain(ctx: ssl.SSLContext) -> None:
    # X509_V_FLAG_PARTIAL_CHAIN доступен на OpenSSL >= 1.1.0 / CPython >= 3.8,
    # но не всегда экспортируется в ssl — используем fallback по битовой маске.
    flag = getattr(ssl, "X509_V_FLAG_PARTIAL_CHAIN", None) or 0x80000
    ctx.verify_flags |= flag


def build_https_session(ca_bundle: str | None = None) -> requests.Session:
    """Создаёт requests.Session для исходящих HTTPS-запросов.

    Порядок доверенных CA:
      1. явно переданная ca_bundle (если указана);
      2. из env MAX_CA_BUNDLE (разделитель os.pathsep) — несколько файлов;
      3. из папки пакета alert_service/certs/max_ca.pem (ships вместе с кодом).
    Наверх разрешено якорение по non-self-issued CA (Minca).
    """
    ctx = ssl.create_default_context()

    candidates: list[str] = []
    if ca_bundle:
        candidates.append(ca_bundle)
    env_bundle = os.getenv("MAX_CA_BUNDLE", "")
    if env_bundle:
        candidates.extend(p for p in env_bundle.split(os.pathsep) if p)
    if _DEFAULT_CA.exists():
        candidates.append(str(_DEFAULT_CA))

    for path in candidates:
        if path and Path(path).exists():
            ctx.load_verify_locations(path)

    _allow_partial_chain(ctx)

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter()
    adapter.poolmanager.connection_pool_kw["ssl_context"] = ctx
    session.mount("https://", adapter)
    return session


class MaxClient:
    BASE_URL = "https://platform-api2.max.ru"

    def __init__(
        self,
        token: str,
        base_url: str | None = None,
        timeout: int = 30,
        ca_bundle: str | None = None,
        session: requests.Session | None = None,
    ):
        if not token:
            raise ValueError("MAX token is empty")
        self.token = token
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.timeout = timeout
        self._session = session if session is not None else build_https_session(ca_bundle=ca_bundle)

    def send_message(
        self,
        text: str,
        chat_id: int | str = None,
        user_id: int | str = None,
        notify: bool = True,
    ):
        if chat_id is None and user_id is None:
            raise ValueError("chat_id или user_id обязательны")

        params = {}
        if chat_id is not None:
            params["chat_id"] = chat_id
        else:
            params["user_id"] = user_id

        body = {
            "text": text,
            "notify": notify,
        }

        r = self._session.post(
            f"{self.base_url}/messages",
            params=params,
            json=body,
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )

        r.raise_for_status()
        return r.json()
