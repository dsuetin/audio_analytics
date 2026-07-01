from .client_state import ClientState
from .session_state import SessionState


class StateManager:

    def __init__(self):

        self.clients = {}
        self.sessions = {}
        self.last_label = None
        self.last_score = 0
        self.threshold_sent = False

    def client(self, client_id):

        if client_id not in self.clients:
            self.clients[client_id] = ClientState()

        return self.clients[client_id]

    def session(self, session_id):

        if session_id not in self.sessions:
            self.sessions[session_id] = SessionState()

        return self.sessions[session_id]

    def finish_session(self, session_id):

        self.sessions.pop(session_id, None)

    def reset_client(self, client_id):

        self.clients.pop(client_id, None)