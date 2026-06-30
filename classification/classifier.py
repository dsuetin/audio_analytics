from collections import Counter

from .policy import (
    BUY_KEYWORDS,
    RETURN_KEYWORDS,
    SERVICE_KEYWORDS,
)


def update(client_state, session_state, text, is_final):

    words = Counter(text.lower().split())

    #
    # partial всегда заменяем
    #
    session_state.partial = words

    #
    # рабочая гистограмма
    #
    session_state.working = (
        client_state.confirmed +
        session_state.partial
    )

    #
    # финал переносим в клиента
    #
    if is_final:

        client_state.confirmed += session_state.partial

        session_state.partial = Counter()

        session_state.working = (
            client_state.confirmed.copy()
        )


def score(histogram: Counter):

    print("\n========== HISTOGRAM ==========")

    for word, count in histogram.most_common():
        print(f"{word:20} {count}")

    print("===============================\n")

    buy = 0
    ret = 0
    svc = 0

    for word, count in histogram.items():

        if word in BUY_KEYWORDS:
            buy += count

        if word in RETURN_KEYWORDS:
            ret += count

        if word in SERVICE_KEYWORDS:
            svc += count

    return buy, ret, svc


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