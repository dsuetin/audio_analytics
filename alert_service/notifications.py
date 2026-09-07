import logging
import os
from abc import ABC, abstractmethod

from alert_service.telegram_bot import TelegramBot
from alert_service.max_bot import MaxClient

logger = logging.getLogger(__name__)


# Абстракция канала отправки уведомлений.
# Business-логика (AlertService) знает только NotificationSender.send(text)
# и не знает деталей API конкретного мессенджера.
#
# Схема:  Alert Service -> NotificationSender -> (Telegram | MAX) API
class NotificationSender(ABC):

    #: человекочитаемое имя канала — для лога
    name = "unknown"

    @abstractmethod
    def send(self, text: str) -> None:
        """Синхронно отправить текст уведомления в настроенный чат/пользователя."""
        raise NotImplementedError


class TelegramSender(NotificationSender):

    name = "telegram"

    def __init__(self, token: str, chat_id: str | int):
        if not token or not chat_id:
            raise ValueError(
                "TelegramSender: TELEGRAM_TOKEN и TELEGRAM_CHAT_ID обязательны"
            )
        self._bot = TelegramBot(token)
        self._chat_id = chat_id

    def send(self, text: str) -> None:
        self._bot.send_message(self._chat_id, text)


class MaxSender(NotificationSender):

    name = "max"

    def __init__(self, token: str, chat_id: str | int):
        chat_id_value = chat_id
        if chat_id_value is None or str(chat_id_value).strip() == "":
            raise ValueError("MaxSender: MAX_CHAT_ID обязателен")
        self._client = MaxClient(token)
        self._chat_id = chat_id_value

    def send(self, text: str) -> None:
        self._client.send_message(text=text, chat_id=self._chat_id)


# ---------------------------------------------------------------------------
# Фабрика: выбирает канал по конфигурации.
#
# Приоритет:
#   1. явный NOTIFICATION_CHANNEL = telegram|max
#   2. если переключатель не задан — telegram, если задан TELEGRAM_TOKEN,
#      иначе max (для backward-совместимости: старые .env с токеном Telegram
#      продолжают работать без изменений)
# ---------------------------------------------------------------------------
def _have_credentials(sender: NotificationSender) -> bool:
    if sender is TelegramSender:
        return bool(os.getenv("TELEGRAM_TOKEN"))
    if sender is MaxSender:
        return bool(os.getenv("MAX_BOT_TOKEN"))
    return False


def create_notification_sender(channel: str | None = None) -> NotificationSender:
    channel = (channel or os.getenv("NOTIFICATION_CHANNEL") or "").strip().lower()

    if channel == "telegram":
        return _build(TelegramSender)
    if channel == "max":
        return _build(MaxSender)

    # нет явного выбора — автоопределение
    if _have_credentials(TelegramSender):
        return _build(TelegramSender)
    if _have_credentials(MaxSender):
        return _build(MaxSender)

    raise RuntimeError(
        "Notification channel is not configured. "
        "Set NOTIFICATION_CHANNEL=telegram|max and the matching credentials: "
        "TELEGRAM_TOKEN+TELEGRAM_CHAT_ID or MAX_BOT_TOKEN+MAX_CHAT_ID."
    )


def _build(sender_cls: type[NotificationSender]) -> NotificationSender:
    if sender_cls is TelegramSender:
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "114987350")
        return TelegramSender(token, chat_id)

    token = os.getenv("MAX_BOT_TOKEN")
    chat_id = os.getenv("MAX_CHAT_ID")
    return MaxSender(token, chat_id)
