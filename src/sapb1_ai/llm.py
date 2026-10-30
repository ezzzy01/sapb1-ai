"""Small helper for background jobs (market watch, briefing) to ask the local model for a written answer."""

import os

import httpx

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("AGENT_BASE_MODEL", "sapb1-stock")


def ask(system: str, prompt: str, max_tokens: int = 900) -> str:
    """One answer from the local model."""
    body = {"model": MODEL, "stream": False, "think": False, "options": {"num_predict": max_tokens},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    r = httpx.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=600)  # includes loading the model
    r.raise_for_status()
    return r.json()["message"]["content"].strip()
