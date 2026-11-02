"""Minimal OpenRouter client: JSON-schema outputs, disk cache, spend guard. Open-weight models only.

The key is read from .env (line `open-router-api-=<key>`) and is never printed or logged.
"""
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "https://openrouter.ai/api/v1"
CACHE = ROOT / "out" / "cache"
BUDGET_USD = 5.0
ANSWERER = "deepseek/deepseek-v4.1-flash"     # DeepSeek family: answers atoms
CHECKER = "qwen/qwen3.8-27b"                  # Qwen family: support check
_key = None


class BudgetExceeded(RuntimeError):
    pass


def _api_key():
    global _key
    if _key is None:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("open-router-api-="):
                _key = line.split("=", 1)[1].strip().strip("\"'")
        if not _key:
            raise RuntimeError("no key in .env")
    return _key


_lock = threading.Lock()
SPEND = CACHE / "spend.json"       # one total for every cache directory


def spent():
    return json.loads(SPEND.read_text())["usd"] if SPEND.exists() else 0.0


def _add_spend(usd):
    with _lock:
        SPEND.parent.mkdir(parents=True, exist_ok=True)
        SPEND.write_text(json.dumps({"usd": spent() + usd}))


def _post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + _api_key(), "Content-Type": "application/json",
        "User-Agent": "contract-twin/0.1"}, method="POST")
    for attempt in range(4):
        try:
            return json.load(urllib.request.urlopen(req, timeout=300))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:300]!r}") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt * 2)


def ask(model, system, user, schema, *, seed=0, temperature=0.0, prompt_version="v1",
        cache_dir=CACHE, use_cache=True, budget=BUDGET_USD):
    """One structured call. Returns dict(data=parsed JSON, model, provider, prompt_version, at, cost, cached)."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps([model, system, user, schema, seed, temperature], sort_keys=True).encode()).hexdigest()
    f = cache_dir / (key + ".json")
    if use_cache and f.exists():
        return {**json.loads(f.read_text(encoding="utf-8")), "cached": True}
    if spent() >= budget:
        raise BudgetExceeded(f"spend {spent():.2f} USD reached budget {budget}")
    body = {"model": model, "temperature": temperature, "seed": seed,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}},
            "provider": {"require_parameters": True}, "usage": {"include": True}}
    for attempt in range(3):     # some providers return empty or cut-off content
        r = _post("/chat/completions", body)
        _add_spend(r.get("usage", {}).get("cost") or 0.0)
        try:
            data = json.loads(r["choices"][0]["message"]["content"])
            break
        except (TypeError, ValueError, KeyError):
            if attempt == 2:
                raise RuntimeError("no valid JSON from model after 3 tries") from None
    usage = r.get("usage", {})
    rec = {"data": data, "model": r.get("model", model),
           "provider": r.get("provider"), "prompt_version": prompt_version,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "cost": usage.get("cost") or 0.0, "tokens": [usage.get("prompt_tokens"), usage.get("completion_tokens")]}
    f.write_text(json.dumps(rec), encoding="utf-8")
    return {**rec, "cached": False}


if __name__ == "__main__":
    sch = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"], "additionalProperties": False}
    r = ask(ANSWERER, "Answer in JSON.", "What is 2+3? Put it in n.", sch, cache_dir=ROOT / "out" / "cache_test")
    assert r["data"]["n"] == 5, r
    print("llm ok", r["model"], r["provider"], r["cost"], "cached" if r["cached"] else "fresh")
