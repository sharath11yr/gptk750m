"""Chat with Gemini (first choice) or NVIDIA (fallback). Returns parsed JSON."""

import json
import logging
import os
import re
import time

import requests
import urllib3.util.connection as _conn

_conn.HAS_IPV6 = False  # IPv4 only (IPv6 can hang on Render)

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_PREFERRED = [
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "deepseek-ai/deepseek-v4.1-flash",
    "deepseek-ai/deepseek-v4.1",
    "qwen/qwen3.5-397b-a17b",
    "openai/gpt-oss-120b",
]
_nvidia_models: list[str] | None = None

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
            flash = [n for n in names if "flash" in n]
            pro = [n for n in names if "pro" in n]
            _gemini_models = (flash[:4] + pro[:1]) or ["gemini-2.5-flash"]
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


def _list_nvidia(headers: dict) -> list[str]:
    """Chat models this key can use right now: preferred ones first, then any other big chat model."""
    global _nvidia_models
    if _nvidia_models is None:
        if forced := os.getenv("NVIDIA_MODEL"):
            _nvidia_models = [forced]
            return _nvidia_models
        try:
            r = requests.get(f"{NVIDIA_URL}/models", headers=headers, timeout=30)
            r.raise_for_status()
            available = [m["id"] for m in r.json().get("data", [])]
        except Exception as exc:
            log.warning("Could not list NVIDIA models: %s", exc)
            available = []
        bad = ("embed", "rerank", "guard", "reward", "vision", "vl", "safety", "parse", "coder",
               "clip", "tts", "asr", "translate")
        others = [m for m in available if not any(b in m.lower() for b in bad)
                  and re.search(r"deepseek|qwen|llama|nemotron|mistral|gpt-oss|kimi|glm", m)]
        _nvidia_models = ([m for m in NVIDIA_PREFERRED if m in available] +
                          [m for m in reversed(others) if m not in NVIDIA_PREFERRED][:6]
                          or NVIDIA_PREFERRED)
    return _nvidia_models


def _nvidia(prompt: str) -> str:
    headers = {"Authorization": f"Bearer {os.environ['NVIDIA_API_KEY']}"}
    last = None
    for model in _list_nvidia(headers):
        try:
            r = requests.post(f"{NVIDIA_URL}/chat/completions", headers=headers, timeout=300,
                              json={"model": model, "temperature": 0.9, "max_tokens": 8000,
                                    "messages": [{"role": "user", "content": prompt}]})
            if r.status_code >= 400:
                raise RuntimeError(f"{model}: HTTP {r.status_code} {r.text[:200]}")
            msg = r.json()["choices"][0]["message"]
            # Reasoning models can leave "content" empty and put the text in reasoning_content.
            text = msg.get("content") or msg.get("reasoning_content") or ""
            if "{" not in text:
                raise RuntimeError(f"{model}: no JSON in the reply")
            _parse(text)  # bad JSON: try the next model
            return text
        except Exception as exc:
            log.warning("NVIDIA failed: %s", exc)
            last = exc
    raise RuntimeError(f"All NVIDIA models failed: {last}")


def _parse(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON in reply")
    body = text[start:end + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", body), strict=False)


def ask_json(prompt: str) -> dict:
    """Ask for a JSON object. Gemini first, then NVIDIA; if both are busy, wait and try again."""
    errors = []
    for wait in (0, 30, 90):
        if wait:
            log.info("All AI models busy; waiting %ds and trying again", wait)
            time.sleep(wait)
        # NVIDIA first (set LLM_ORDER=gemini,nvidia to change)
        order = (os.getenv("LLM_ORDER") or "nvidia,gemini").split(",")
        providers = {"gemini": _gemini, "nvidia": _nvidia}
        for name, fn in ((n.strip(), providers[n.strip()]) for n in order if n.strip() in providers):
            if not os.getenv(f"{name.upper()}_API_KEY"):
                continue
            try:
                return _parse(fn(prompt))
            except Exception as exc:
                errors.append(f"{name}: {str(exc)[:300]}")
    raise RuntimeError("LLM failed: " + " | ".join(errors[-2:]))
