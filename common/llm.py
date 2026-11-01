"""Chat with Gemini (first choice) or NVIDIA (fallback). Returns parsed JSON."""

import json
import logging
import os
import re

import requests
import urllib3.util.connection as _conn

_conn.HAS_IPV6 = False  # IPv4 only (IPv6 can hang on Render)

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODELS = [
    "deepseek-ai/deepseek-v3.2",
    "deepseek-ai/deepseek-v3.1",
    "qwen/qwen3-235b-a22b",
    "moonshotai/kimi-k2-instruct",
    "meta/llama-4-maverick-17b-128e-instruct",
    "meta/llama-3.3-70b-instruct",
]

_gemini_models: list[str] | None = None


def _version(name: str) -> float:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def _list_gemini() -> list[str]:
    """Newest 'flash' and 'pro' text models this key can use, best first."""
    global _gemini_models
    if _gemini_models is None:
        if forced := os.getenv("GEMINI_MODEL"):
            _gemini_models = [forced]
            return _gemini_models
        try:
            r = requests.get(f"{GEMINI_URL}/models", params={"key": os.environ["GEMINI_API_KEY"],
                                                          "pageSize": 200}, timeout=30)
            r.raise_for_status()
            names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
            bad = ("lite", "image", "tts", "live", "audio", "embed", "thinking", "exp", "nano")
            names = [n for n in names if n.startswith("gemini-") and not any(b in n for b in bad)
                     and ("flash" in n or "pro" in n)]
            # Prefer the newest version; within a version prefer flash (fast, generous free tier).
            names.sort(key=lambda n: (_version(n), "flash" in n, "latest" not in n), reverse=True)
            _gemini_models = names[:4] or ["gemini-2.5-flash"]
        except Exception as exc:
            log.warning("Could not list Gemini models: %s", exc)
            _gemini_models = ["gemini-2.5-flash", "gemini-2.0-flash"]
    return _gemini_models


def _gemini(prompt: str) -> str:
    last = None
    for model in _list_gemini():
        try:
            r = requests.post(f"{GEMINI_URL}/models/{model}:generateContent",
                              params={"key": os.environ["GEMINI_API_KEY"]}, timeout=180,
                              json={"contents": [{"parts": [{"text": prompt}]}],
                                    "generationConfig": {"temperature": 0.9,
                                                         "responseMimeType": "application/json"}})
            if r.status_code >= 400:
                raise RuntimeError(f"{model}: HTTP {r.status_code} {r.text[:200]}")
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except Exception as exc:
            log.warning("Gemini failed: %s", exc)
            last = exc
    raise RuntimeError(f"All Gemini models failed: {last}")


def _nvidia(prompt: str) -> str:
    headers = {"Authorization": f"Bearer {os.environ['NVIDIA_API_KEY']}"}
    models = [os.environ["NVIDIA_MODEL"]] if os.getenv("NVIDIA_MODEL") else NVIDIA_MODELS
    last = None
    for model in models:
        try:
            r = requests.post(f"{NVIDIA_URL}/chat/completions", headers=headers, timeout=300,
                              json={"model": model, "temperature": 0.9, "max_tokens": 4096,
                                    "messages": [{"role": "user", "content": prompt}]})
            if r.status_code >= 400:
                raise RuntimeError(f"{model}: HTTP {r.status_code} {r.text[:200]}")
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning("NVIDIA failed: %s", exc)
            last = exc
    raise RuntimeError(f"All NVIDIA models failed: {last}")


def _parse(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON in reply")
    return json.loads(text[start:end + 1])


def ask_json(prompt: str) -> dict:
    """Ask for a JSON object. Gemini first, then NVIDIA."""
    errors = []
    for name, fn in (("gemini", _gemini), ("nvidia", _nvidia)):
        if not os.getenv(f"{name.upper()}_API_KEY"):
            continue
        for _ in range(2):
            try:
                return _parse(fn(prompt))
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    raise RuntimeError("LLM failed: " + " | ".join(errors[-3:]))
