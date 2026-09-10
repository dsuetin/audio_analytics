from collections import Counter

from .policy import MISSIONS, THRESHOLD
from .phrase_matcher import find_phrases


#
# MISSIONS:
#
# {
#   "buy": [
#       "фраза1",
#       "фраза2"
#   ],
#   ...
# }
#
# Собираем обратный индекс:
# фраза -> миссия
#
PHRASE_TO_MISSION = {}

for mission, phrases in MISSIONS.items():
    for phrase in phrases:
        PHRASE_TO_MISSION[phrase] = mission


ALL_PHRASES = list(PHRASE_TO_MISSION.keys())


def update(
    client_state,
    session_state,
    text,
    is_final,
):

    found = find_phrases(
        text,
        ALL_PHRASES,
    )

    #
    # сохраняем гистограмму фраз
    #
    session_state.partial = Counter(found)

    if is_final:
        client_state.confirmed += session_state.partial
        session_state.partial.clear()



def score(histogram: Counter):
    scores = {
        mission: 0
        for mission in MISSIONS.keys()
    }

    matched = {
        mission: {}
        for mission in MISSIONS.keys()
    }


    #
    # считаем очки каждой миссии
    #
    for phrase, count in histogram.items():

        mission = PHRASE_TO_MISSION.get(
            phrase
        )

        if mission is None:
            continue


        scores[mission] += count

        matched[mission][phrase] = count



    return scores, matched



def best_label(scores: dict[str, int]):

    if not scores:
        return None, 0


    label, value = max(
        scores.items(),
        key=lambda x: x[1],
    )

    return label, value



def threshold_hit(scores: dict[str, int]):

    return max(
        scores.values(),
        default=0,
    ) >= THRESHOLD