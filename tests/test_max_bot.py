import requests


class MaxBot:
    BASE_URL = "https://platform-api2.max.ru"

    def __init__(self, token: str):
        self.token = token

    def send_message(
        self,
        text: str,
        user_id: int = None,
        chat_id: int = None,
        markdown: bool = False,
        notify: bool = True,
    ):
        if not user_id and not chat_id:
            raise ValueError("Необходимо указать user_id или chat_id")

        params = {}

        if user_id:
            params["user_id"] = user_id

        if chat_id:
            params["chat_id"] = chat_id

        body = {
            "text": text,
            "notify": notify,
        }

        if markdown:
            body["format"] = "markdown"

        r = requests.post(
            f"{self.BASE_URL}/messages",
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
            },
            params=params,
            json=body,
            timeout=30,
        )

        r.raise_for_status()

        return r.json()


if __name__ == "__main__":
    bot = MaxBot("YOUR_BOT_TOKEN")

    result = bot.send_message(
        user_id=123456789,
        text="Тестовое сообщение"
    )

    print(result)