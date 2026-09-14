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
        self.model = model or os.getenv("OFFLINE_LLM_MODEL", "Qwen3.5-27B-UD-Q4_K_XL.gguf")
        self.host = (host or os.getenv("LLAMACPP_HOST", "http://172.16.20.111:8082/v1")).rstrip("/")
        self.timeout = timeout if timeout is not None else int(os.getenv("OFFLINE_LLM_TIMEOUT", "1200"))

    def chat_json(
        self,
        system: str,
        user: str,
        temperature: float = 0.0,
        num_ctx: int = 64000,
        num_predict: int = 8192,
        think: bool = False,
        retries: int = 3,
    ) -> dict:
        # think=False обязательно: thinking-модель «съедает» num_predict на
        # размышление и в content может не остаться JSON. Для детерминированного
        # JSON-контракта (сегментация/классификация) рассуждения отключаем.
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        # Отключаем thinking/reasoning для Qwen3.5 через chat_template_kwargs
        if not think:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            print(f"[LLM] Thinking disabled via chat_template_kwargs")
        
        last_error = None
        for attempt in range(retries + 1):
            try:
                resp = requests.post(self.host + "/chat/completions", json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    break
                last_error = f"llama.cpp HTTP {resp.status_code}: {resp.text[:500]}"
            except requests.RequestException as exc:
                last_error = f"llama.cpp request failed (attempt {attempt + 1}/{retries + 1}): {exc}"
        
        if last_error:
            raise OllamaError(last_error)

        data = resp.json()
        content = ""
        choices = data.get("choices", [])
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            if isinstance(message, dict):
                content = message.get("content", "")
        if not content:
            content = data.get("response", "")
        return {"content": content, "raw": data}


def extract_json(text: str) -> dict:
    """Достаёт первый JSON-объект из ответа модели (допускает markdown, noise и
    обрывы по num_predict)."""
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

    # Fallback: обрыв по num_predict. Находим начало объекта и пытаемся закрыть
    # «висящие» элементы массива/объекта, чтобы получить частичный, но полезный ответ.
    for cand in candidates:
        start = cand.find('{')
        if start == -1:
            continue
        frag = cand[start:]
        open_obj = frag.count('{') - frag.count('}')
        open_arr = frag.count('[') - frag.count(']')
        if open_obj < 0 or open_arr < 0:
            continue
        closed = frag.rstrip().rstrip(',"')
        closed = closed + ']' * max(open_arr, 0) + '}' * max(open_obj, 0)
        try:
            obj = json.loads(closed)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return {}
