from collections import Counter


class ClientState:

    def __init__(self):

        # подтвержденная история клиента
        self.confirmed = Counter()