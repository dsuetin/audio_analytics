from collections import Counter

import snowballstemmer


stemmer = snowballstemmer.stemmer("russian")


def normalize_words(text: str):

    return [
        stemmer.stemWord(word)
        for word in text.lower().split()
    ]


def phrase_count(text: str, phrase: str):

    text_words = normalize_words(text)
    phrase_words = normalize_words(phrase)

    n = len(phrase_words)

    if n > len(text_words):
        return 0

    count = 0

    for i in range(len(text_words) - n + 1):

        if text_words[i:i + n] == phrase_words:
            count += 1

    return count


def find_phrases(text, phrases):

    result = Counter()

    for phrase in phrases:

        count = phrase_count(text, phrase)

        if count:
            result[phrase] = count

    return result