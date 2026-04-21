"""
NVIDIA NIM Client
Primary  : moonshotai/kimi-k2-thinking   (best quality, 40 req/min free)
Secondary: stepfun-ai/step-3.5-flash     (fast, scoring/classification)
Fallback : mistralai/devstral-2-123b-instruct-2512

All models free via https://integrate.api.nvidia.com/v1
Get key: https://build.nvidia.com/settings/api-keys
"""

import time, logging, requests, threading
from typing import Optional

log = logging.getLogger("nim")

NIM_BASE = "https://integrate.api.nvidia.com/v1"

DEFAULT_MODELS = {
    # Non-thinking models only. <think> emitters (kimi-k2-thinking,
    # deepseek-r1, qwq) ship raw reasoning when max_tokens truncates
    # mid-thought. See _strip_reasoning below for the defence in depth.
    "primary"  : "mistralai/devstral-2-123b-instruct-2512",
    "fast"     : "meta/llama-3.3-70b-instruct",
    "fallback" : "z-ai/glm4.7",
}

class _RateLimiter:
    """Token bucket — max 35 calls/min (buffer below 40 NIM limit)."""
    def __init__(self, max_per_min=35):
        self._max    = max_per_min
        self._tokens = float(max_per_min)
        self._lock   = threading.Lock()
        self._last   = time.monotonic()

    def acquire(self):
        with self._lock:
            now     = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._max, self._tokens + elapsed * (self._max / 60.0))
            self._last   = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / (self._max / 60.0)
                log.info(f"[NIM] Rate limit — waiting {wait:.1f}s")
                time.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1

_RATE_LIMITER = _RateLimiter(max_per_min=35)


import re as _re_llm

_THINK_PATTERNS = [
    # Closed pairs (non-greedy, case-insensitive, spans newlines).
    _re_llm.compile(r"<think>.*?</think>",       _re_llm.DOTALL | _re_llm.IGNORECASE),
    _re_llm.compile(r"<thinking>.*?</thinking>", _re_llm.DOTALL | _re_llm.IGNORECASE),
    _re_llm.compile(r"<\|begin_of_thought\|>.*?<\|end_of_thought\|>", _re_llm.DOTALL),
]

_THINK_OPEN_TAGS = (
    "<think>", "<thinking>", "<|begin_of_thought|>",
)


def _strip_reasoning(text: str) -> str:
    """
    Remove reasoning-model scaffolding from model output:
      * fully paired <think>...</think> blocks (and variants)
      * unclosed <think>... blocks when max_tokens cut off mid-thought
        (everything from the opening tag to end-of-string is discarded)
    Raises nothing; returns the cleaned text. Empty-output guard lives in _call.
    """
    if not text:
        return ""
    # 1. Strip all closed pairs first.
    for pat in _THINK_PATTERNS:
        text = pat.sub("", text)
    # 2. Handle unclosed opening tags — anything after them is reasoning that
    #    never reached a closing marker, so it's unreliable.
    lowered = text.lower()
    cut = len(text)
    for tag in _THINK_OPEN_TAGS:
        idx = lowered.find(tag.lower())
        if idx >= 0 and idx < cut:
            cut = idx
    if cut < len(text):
        text = text[:cut]
    return text.strip()


class NIMClient:
    def __init__(self, api_key: str, models: Optional[dict] = None):
        self.api_key  = api_key
        self.models   = {**DEFAULT_MODELS, **(models or {})}
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type" : "application/json",
        }

    def _call(self, model: str, messages: list, max_tokens=2048,
              temperature=0.7, retries=3) -> str:
        payload = {
            "model"      : model,
            "messages"   : messages,
            "max_tokens" : max_tokens,
            "temperature": temperature,
            "stream"     : False,
        }
        _RATE_LIMITER.acquire()
        for attempt in range(retries):
            try:
                r = requests.post(
                    f"{NIM_BASE}/chat/completions",
                    headers=self._headers,
                    json=payload,
                    timeout=60,
                )
                if r.status_code == 429:
                    wait = 2 ** attempt * 15
                    log.warning(f"[NIM] 429 rate limit on {model} — sleeping {wait}s")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data    = r.json()
                choices = data.get("choices") or []
                if not choices:
                    raise ValueError(f"Empty choices: {data}")
                msg     = choices[0].get("message") or {}
                content = msg.get("content") or ""
                if not content:
                    content = msg.get("reasoning_content") or msg.get("reasoning") or ""
                if not content:
                    raise ValueError(f"Empty content and reasoning: {msg}")
                # Strip <think>...</think> scaffolding (closed or truncated).
                cleaned = _strip_reasoning(content)
                if len(cleaned) < 30:
                    raise ValueError(
                        f"Model output was all reasoning, no substantive content "
                        f"after stripping (len={len(cleaned)}, model={model})"
                    )
                return cleaned
            except requests.exceptions.RequestException as e:
                log.warning(f"[NIM] Attempt {attempt+1} failed ({model}): {e}")
                if attempt < retries - 1:
                    time.sleep(5)
        raise RuntimeError(f"NIM call failed after {retries} retries on {model}")

    def complete(self, prompt: str, tier="primary", max_tokens=2048,
                 temperature=0.7) -> tuple[str, str]:
        order    = {
            "primary" : ["primary", "fast", "fallback"],
            "fast"    : ["fast", "primary", "fallback"],
            "fallback": ["fallback", "fast", "primary"],
        }.get(tier, ["primary", "fast", "fallback"])
        messages = [{"role": "user", "content": prompt}]
        for tier_name in order:
            model = self.models.get(tier_name)
            if not model:
                continue
            try:
                return self._call(model, messages, max_tokens, temperature), model
            except Exception as e:
                log.warning(f"[NIM] {tier_name} ({model}) failed: {e} — trying next")
        raise RuntimeError("All NIM models failed.")

    def complete_system(self, system: str, user: str, tier="primary",
                        max_tokens=2048, temperature=0.7) -> tuple[str, str]:
        order    = {
            "primary" : ["primary", "fast", "fallback"],
            "fast"    : ["fast", "primary", "fallback"],
        }.get(tier, ["primary", "fast", "fallback"])
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ]
        for tier_name in order:
            model = self.models.get(tier_name)
            if not model:
                continue
            try:
                return self._call(model, messages, max_tokens, temperature), model
            except Exception as e:
                log.warning(f"[NIM] {tier_name} failed: {e}")
        raise RuntimeError("All NIM models failed.")
