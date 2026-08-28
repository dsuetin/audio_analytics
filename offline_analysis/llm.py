from __future__ import annotations

import json
import os

import requests


OllamaError = ConnectionError


class OllamaClient:
    """Минимальный клиент Ollama /api/chat.

    Используется ТОЛЬКО offline-анализом (реалтайм-контур не затрагивается).
    """

    def __init__(self, model: str | None = None, host: str | None = None, timeout: int | None = None):
        self.model = model or os.getenv("OFFLINE_LLM_MODEL", "qwen3.8:27b")
        self.host = (host or os.getenv("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.timeout = timeout if timeout is not None else int(os.getenv("OFFLINE_LLM_TIMEOUT", "600"))

    def chat_json(
        self,
        system: str,
        user: str,
        temperature: float = 0.0,
        num_ctx: int = 64000,
        num_predict: int = 8192,
    ) -> dict:
        # Модель в thinking-режиме: num_predict должен покрывать и «размышление», и JSON.
        payload = {
            "model": self.model,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx,
                "num_predict": num_predict,
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = requests.post(self.host + "/api/chat", json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc

        if resp.status_code != 200:
            raise OllamaError(f"Ollama HTTP {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        content = ""
        message = data.get("message")
        if isinstance(message, dict):
            content = message.get("content", "")
        if not content:
            content = data.get("response", "")
        return {"content": content, "raw": data}


def extract_json(text: str) -> dict:
    """Достаёт первый JSON-объект из ответа модели (допускает markdown/noise)."""
    text = (text or "").strip()
    candidates = [text]
    if "```" in text:
        for chunk in text.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            candidates.append(chunk)
    for cand in candidates:
        start = cand.find("{")
        end = cand.rfind("}")
        if start != -1 and end > start:
            frag = cand[start:end + 1]
            try:
                obj = json.loads(frag)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return {}
