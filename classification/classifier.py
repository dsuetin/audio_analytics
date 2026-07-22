from collections import Counter

from .policy import (
    BUY_KEYWORDS,
    RETURN_KEYWORDS,
    SERVICE_KEYWORDS,
)


from .phrase_matcher import find_phrases


def update(client_state, session_state, text, is_final):

    all_keywords = BUY_KEYWORDS.union(RETURN_KEYWORDS).union(SERVICE_KEYWORDS)
    phrases = find_phrases(
        text,
        all_keywords,
    )
    session_state.partial = phrases
    if is_final:
        client_state.confirmed += session_state.partial
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

    label, _ = max(scores.items(), key=lambda item: item[1])

    return label, scores[label]


def threshold_hit(buy, ret, svc):

    return max(buy, ret, svc) >= 5