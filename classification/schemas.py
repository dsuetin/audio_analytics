from pydantic import BaseModel


class ASREvent(BaseModel):
    session_id: str
    text: str
    is_final: bool = True


class ClassifiedEvent(BaseModel):
    session_id: str
    text: str
    label: str   # buy / return / service / unknown
    score: float = 1.0