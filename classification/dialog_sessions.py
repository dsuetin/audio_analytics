GREETINGS = (
    "здравствуйте",
    "добрый день",
    "добрый вечер",
    "доброе утро",
    "привет",
    "приветствую",
)

FAREWELLS = (
    "до свидания",
    "до свиданья",
    "всего доброго",
    "всего хорошего",
    "до встречи",
    "счастливо",
    "хорошего дня",
	"хорошего вечера",
    "заходите еще",
)


class DialogSession:

    def __init__(self):
        self.closed = False

    def process(self, text: str, is_final: bool) -> bool:
        """
        Возвращает True, если началась новая сессия.
        """

        if not is_final:
            return False

        text = text.lower()

        #
        # клиент попрощался
        #
        if any(p in text for p in FAREWELLS):
            self.closed = True
            return False

        #
        # после прощания снова поздоровались
        #
        if self.closed and any(p in text for p in GREETINGS):
            self.closed = False
            return True

        return False