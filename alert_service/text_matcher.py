import snowballstemmer

from alert_service.config import MAX_GAP

stemmer = snowballstemmer.stemmer("russian")


def normalize_words(text: str):
    words = text.lower().split()

    return [
        stemmer.stemWord(w)
        for w in words
    ]


def phrase_match(
    text: str,
    phrase: str,
    max_gap: int = MAX_GAP,
):
    text_words = normalize_words(text)
    phrase_words = normalize_words(phrase)

    if not phrase_words:
        return False

    text_len = len(text_words)
    phrase_len = len(phrase_words)


    # стартуем с первого слова фразы
    for start in range(text_len):

        if text_words[start] != phrase_words[0]:
            continue


        pos = start + 1
        phrase_pos = 1


        while phrase_pos < phrase_len:

            found = False


            # ищем следующее слово фразы
            # в пределах MAX_GAP слов
            for gap in range(max_gap + 1):

                idx = pos + gap

                if idx >= text_len:
                    break


                if text_words[idx] == phrase_words[phrase_pos]:
                    pos = idx + 1
                    phrase_pos += 1
                    found = True
                    break


            if not found:
                break


        if phrase_pos == phrase_len:
            return True


    return False



def find_phrase(
    text: str,
    phrases,
    max_gap: int = MAX_GAP,
):

    for phrase in phrases:

        if phrase_match(
            text,
            phrase,
            max_gap,
        ):
            return phrase

    return None