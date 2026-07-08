import requests


class TelegramBot:
    def __init__(self, token : str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }

        if parse_mode:
            payload["parse_mode"] = parse_mode

        r = requests.post(
            f"{self.base_url}/sendMessage",
            json=payload,
            timeout=30,
        )

        r.raise_for_status()
        return r.json()
