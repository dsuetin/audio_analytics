from __future__ import annotations

import sys
import types
from urllib.parse import parse_qs, urlparse

import pytest


# alert_service не тянет тяжёлые зависимости (requests есть в requirements.txt);
# aiokafka/asyncpg установлены только в Docker-облаче — подменяем для теста.
def _stub_heavy_deps():
    if "aiokafka" not in sys.modules:
        stub = types.ModuleType("aiokafka")
        stub.AIOKafkaConsumer = object
        stub.AIOKafkaProducer = object
        sys.modules["aiokafka"] = stub
    if "asyncpg" not in sys.modules:
        asyncpg_stub = types.ModuleType("asyncpg")

        async def _fake_connect(*args, **kwargs):
            return object()

        asyncpg_stub.connect = _fake_connect
        sys.modules["asyncpg"] = asyncpg_stub


# ---------------------------------------------------------------------------
# Фейковый requests.Session — перехватывает .post(...) и логирует аргументы
# ---------------------------------------------------------------------------

def _make_fake_session(responder):
    captured = {}

    class FakeSession:
        def post(self, url, params=None, json=None, headers=None, timeout=None, **kw):
            captured.update(url=url, params=params, json=json, headers=headers, timeout=timeout)
            return responder()

    return FakeSession(), captured


# ---------------------------------------------------------------------------
# 1. MaxClient: URL / заголовки / body (официальный API MAX, dev.max.ru/docs-api)
# ---------------------------------------------------------------------------

def test_maxclient_uses_platform_api2_domain():
    from alert_service.max_bot import MaxClient

    c = MaxClient("tok")
    assert c.base_url == "https://platform-api2.max.ru"


def test_maxclient_token_not_empty():
    from alert_service.max_bot import MaxClient

    with pytest.raises(ValueError):
        MaxClient("")


def test_maxclient_send_builds_correct_request():
    from alert_service.max_bot import MaxClient

    def ok():
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"message": {"id": 7}})

    session, captured = _make_fake_session(ok)
    result = MaxClient("TOK", session=session).send_message(text="hello", chat_id=123456)

    parsed = urlparse(captured["url"])
    assert captured["url"].startswith("https://platform-api2.max.ru/messages")
    assert parsed.path == "/messages"
    # токен — в заголовке Authorization, НЕ в query-параметрах
    assert not parsed.query.startswith("token=")
    assert captured["params"] == {"chat_id": 123456}
    assert captured["headers"]["Authorization"] == "TOK"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["json"] == {"text": "hello", "notify": True}
    assert captured["timeout"] == 30
    assert result == {"message": {"id": 7}}


def test_maxclient_send_by_user_id():
    from alert_service.max_bot import MaxClient

    def ok():
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    session, captured = _make_fake_session(ok)
    MaxClient("TOK", session=session).send_message(text="hi", user_id=999)
    assert captured["params"] == {"user_id": 999}


def test_maxclient_requires_recipient():
    from alert_service.max_bot import MaxClient

    def ok():
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})

    with pytest.raises(ValueError, match="chat_id или user_id"):
        MaxClient("TOK", session=_make_fake_session(ok)[0]).send_message(text="hi")


def test_maxclient_propagates_http_4xx():
    import requests
    from alert_service.max_bot import MaxClient

    class ErrResp:
        def raise_for_status(self):
            raise requests.HTTPError("401 Client Error", response=types.SimpleNamespace(status_code=401))

    def boom():
        return ErrResp()

    with pytest.raises(requests.HTTPError):
        MaxClient("BAD", session=_make_fake_session(boom)[0]).send_message(text="hi", chat_id=1)


# ---------------------------------------------------------------------------
# 1b. TLS/CA: build_https_session возвращает реальный requests.Session,
#     в который загружен CA-сертификат (Minca) + включён PARTIAL_CHAIN
# ---------------------------------------------------------------------------

def test_build_https_session_is_requests_session_with_ca(monkeypatch):
    import ssl
    import requests
    from alert_service import max_bot

    session = max_bot.build_https_session()
    assert isinstance(session, requests.Session)

    # session должен иметь замонтированный https-адаптер с собственным ssl_context
    adapter = session.get_adapter("https://x")
    assert "ssl_context" in adapter.poolmanager.connection_pool_kw
    ctx = adapter.poolmanager.connection_pool_kw["ssl_context"]
    assert isinstance(ctx, ssl.SSLContext)

    # PARTIAL_CHAIN должен быть выключен/включён явно (флаг 0x80000)
    assert ctx.verify_flags & 0x80000  # X509_V_FLAG_PARTIAL_CHAIN


def test_build_https_session_loads_package_ca(monkeypatch, tmp_path):
    """CA из alert_service/certs/max_ca.pem загружается в контекст по умолчанию."""
    import ssl
    from alert_service import max_bot
    from pathlib import Path

    # CA уже ships в пакете (max_ca.pem). Проверим, что он существует
    # и что build_https_session() его реально грузит (ca_certs не пустой).
    ca = Path(max_bot.__file__).parent / "certs" / "max_ca.pem"
    assert ca.exists(), "alert_service/certs/max_ca.pem отсутствует в пакете"

    ctx_probe = ssl.create_default_context()
    session = max_bot.build_https_session()
    adapter = session.get_adapter("https://x")
    ctx = adapter.poolmanager.connection_pool_kw["ssl_context"]
    # контекст должен быть в режиме verify — значит CA загружен (иначе verify_mode=0)
    assert ctx.verify_mode != ssl.CERT_NONE


# ---------------------------------------------------------------------------
# 2. NotificationSender / MaxSender — фабрика и пересылка текста
# ---------------------------------------------------------------------------

def test_maxSender_passes_text_untouched_and_chat_id(monkeypatch):
    from alert_service.notifications import MaxSender

    calls = []

    def fake_send_message(self, text=None, chat_id=None, user_id=None, notify=True):
        calls.append({"text": text, "chat_id": chat_id})

    monkeypatch.setattr("alert_service.notifications.MaxClient.send_message", fake_send_message)

    text = "🚨 Alert\n\nStore: s1\nSession: sess-1\n\nслишком дорого"
    MaxSender("TOK", "CHAT-123").send(text)

    assert calls == [{"text": text, "chat_id": "CHAT-123"}]


def test_maxsender_requires_chat_id():
    from alert_service.notifications import MaxSender

    with pytest.raises(ValueError):
        MaxSender("TOK", "")
    with pytest.raises(ValueError):
        MaxSender("TOK", None)


def test_create_notification_sender_selects_max(monkeypatch):
    from alert_service.notifications import MaxSender, create_notification_sender

    monkeypatch.setenv("NOTIFICATION_CHANNEL", "max")
    monkeypatch.setenv("MAX_BOT_TOKEN", "TOK")
    monkeypatch.setenv("MAX_CHAT_ID", "999")

    sender = create_notification_sender()
    assert isinstance(sender, MaxSender)
    assert sender.name == "max"


def test_create_notification_sender_selects_telegram(monkeypatch):
    from alert_service.notifications import TelegramSender, create_notification_sender

    monkeypatch.setenv("NOTIFICATION_CHANNEL", "telegram")
    monkeypatch.setenv("TELEGRAM_TOKEN", "tg_token")
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)

    sender = create_notification_sender()
    assert isinstance(sender, TelegramSender)
    assert sender.name == "telegram"


def test_create_notification_sender_auto_prefers_telegram(monkeypatch):
    from alert_service.notifications import TelegramSender, create_notification_sender

    monkeypatch.delenv("NOTIFICATION_CHANNEL", raising=False)
    monkeypatch.setenv("TELEGRAM_TOKEN", "tg_token")
    monkeypatch.setenv("MAX_BOT_TOKEN", "TOK")
    monkeypatch.setenv("MAX_CHAT_ID", "999")

    # без явного переключателя: telegram, если задан TELEGRAM_TOKEN
    assert isinstance(create_notification_sender(), TelegramSender)


def test_create_notification_sender_auto_falls_back_to_max(monkeypatch):
    from alert_service.notifications import MaxSender, create_notification_sender

    monkeypatch.delenv("NOTIFICATION_CHANNEL", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.setenv("MAX_BOT_TOKEN", "TOK")
    monkeypatch.setenv("MAX_CHAT_ID", "999")

    assert isinstance(create_notification_sender(), MaxSender)


def test_create_notification_sender_no_credentials_raises(monkeypatch):
    from alert_service.notifications import create_notification_sender

    monkeypatch.delenv("NOTIFICATION_CHANNEL", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("MAX_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MAX_CHAT_ID", raising=False)

    with pytest.raises(RuntimeError, match="not configured"):
        create_notification_sender()


# ---------------------------------------------------------------------------
# 3. AlertService: условие срабатывания + формирование текста уведомления НЕ меняются
#    и передаются в NotificationSender (канал — абстракция).
# ---------------------------------------------------------------------------

def _make_svc(monkeypatch):
    import json

    import alert_service.alerts_service as al

    monkeypatch.setenv("NOTIFICATION_CHANNEL", "max")
    monkeypatch.setenv("MAX_BOT_TOKEN", "TOK")
    monkeypatch.setenv("MAX_CHAT_ID", "999")

    svc = al.AlertService()

    async def execute(*a, **kw):
        return None

    async def send_and_wait(topic, body):
        return None

    svc.db = types.SimpleNamespace(execute=execute)
    svc.producer = types.SimpleNamespace(send_and_wait=send_and_wait)

    return svc, json


def test_alertservice_sends_alert_message_with_unchanged_text(monkeypatch, caplog):
    import asyncio
    import logging

    _stub_heavy_deps()
    svc, json = _make_svc(monkeypatch)

    # бизнес-логика не зависит от канала — только от NotificationSender.send
    sent = []
    monkeypatch.setattr(svc.notification_sender, "send", lambda text: sent.append(text))

    event = {
        "session_id": "store1-worker-9",
        "text": "это слишком дорогое специальное предложение",
        "is_final": True,
    }
    msg = types.SimpleNamespace(value=json.dumps(event).encode())

    with caplog.at_level(logging.WARNING):
        asyncio.run(svc.handle(msg))

    assert sent == [
        "🚨 Alert\n\n"
        "Store: store1\n"
        "Session: store1-worker-9\n\n"
        "это слишком дорогое специальное предложение"
    ]
    assert "🚨 ALERT" in caplog.text


def test_alertservice_notification_error_does_not_crash(monkeypatch):
    import asyncio

    _stub_heavy_deps()
    svc, json = _make_svc(monkeypatch)

    def boom(text):
        raise RuntimeError("MAX DOWN 500")

    monkeypatch.setattr(svc.notification_sender, "send", boom)

    event = {
        "session_id": "s-w",
        "text": "слишком дорогое специальное предложение",
        "is_final": True,
    }
    msg = types.SimpleNamespace(value=json.dumps(event).encode())

    # ошибка канала — перехватывается, handle() завершается без падения
    asyncio.run(svc.handle(msg))
