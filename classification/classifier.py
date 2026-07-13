from collections import Counter

from .policy import (
    BUY_KEYWORDS,
    RETURN_KEYWORDS,
    SERVICE_KEYWORDS,
)


def update(client_state, session_state, text, is_final):

    words = Counter(text.lower().split())

    session_state.partial = words

    if is_final:
        client_state.confirmed += words
        session_state.partial.clear()


def score(histogram: Counter):

    print("\n========== HISTOGRAM ==========")

    for word, count in histogram.most_common():
        print(f"{word:20} {count}")

    print("===============================\n")

    buy = 0
    ret = 0
    svc = 0
    buy_words = {}
    return_words = {}
    service_words = {}

    for word, count in histogram.items():

        if word in BUY_KEYWORDS:
            buy += count
            buy_words[word] = count

        if word in RETURN_KEYWORDS:
            ret += count
            return_words[word] = count

        if word in SERVICE_KEYWORDS:
            svc += count
            service_words[word] = count

    return (
        buy,
        ret,
        svc,
        {
            "buy": buy_words,
            "return": return_words,
            "service": service_words,
        },
    )


def best_label(buy, ret, svc):

    scores = {
        "buy": buy,
        "return": ret,
        "service": svc,
    }

    label = max(scores, key=scores.get)

    return label, scores[label]


def threshold_hit(buy, ret, svc):

    return max(buy, ret, svc) >= 5