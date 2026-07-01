from collections import Counter


class SessionState:

    def __init__(self):

        # последний partial
        self.partial = Counter()

        # confirmed клиента + partial
        self.working = Counter()

        self.last_label = None
        self.last_score = 0

        self.threshold_sent = False