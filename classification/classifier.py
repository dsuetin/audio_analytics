from collections import defaultdict
from typing import Dict, Tuple
from .config import SCENARIOS, THRESHOLD


class SessionClassifier:
    def __init__(self):
        self.hist: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.final_scenario = {}
        self.finalized = set()

    def update(self, session_id: str, text: str) -> Tuple[str, int, bool]:
        words = text.lower().split()

        for w in words:
            for scenario, vocab in SCENARIOS.items():
                if w in vocab:
                    self.hist[session_id][scenario] += 1

        best_scenario, score = self._best(session_id)

        prev = self.final_scenario.get(session_id)

        # threshold trigger
        if score >= THRESHOLD and best_scenario != prev:
            self.final_scenario[session_id] = best_scenario
            return best_scenario, score, False

        # switch if stronger scenario appears
        if prev and best_scenario != prev and score > self.hist[session_id][prev]:
            self.final_scenario[session_id] = best_scenario
            return best_scenario, score, False

        return best_scenario, score, False

    def finalize(self, session_id: str):
        scenario, score = self._best(session_id)
        self.final_scenario[session_id] = scenario
        self.finalized.add(session_id)
        return scenario, score

    def _best(self, session_id: str):
        data = self.hist[session_id]
        if not data:
            return "unknown", 0
        return max(data.items(), key=lambda x: x[1])