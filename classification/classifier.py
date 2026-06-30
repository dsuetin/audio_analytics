from collections import Counter

from .policy import BUY_KEYWORDS, RETURN_KEYWORDS, SERVICE_KEYWORDS


def update(state, text: str, is_final: bool):

    words = text.lower().split()

    if is_final:
        # подтверждаем последнюю гипотезу
        state.confirmed += state.last_partial
        state.last_partial = Counter()
    else:
        # заменяем предыдущую гипотезу
        state.last_partial = Counter(words)

    state.working = state.confirmed + state.last_partial


def score(state):

    buy = sum(
        c for w, c in state.working.items()
        if w in BUY_KEYWORDS
    )

    ret = sum(
        c for w, c in state.working.items()
        if w in RETURN_KEYWORDS
    )

    svc = sum(
        c for w, c in state.working.items()
        if w in SERVICE_KEYWORDS
    )

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