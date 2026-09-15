#!/usr/bin/env python3
"""모델 호출 — llama-server /completion 에 json_schema(GBNF)로 4축 JSON을 강제한다.

모델이 하는 일은 여기까지다. 판정·이동은 rules.py 가 한다(ADR-0007).

- `cache_prompt=False` — 서류 1건마다 KV 캐시를 새로 연다(무상태 세션, 기획서 §3.1). 발열·메모리를 설계로 잡는다.
- `temperature=0` — 같은 파일이면 같은 라벨.
- 스키마가 거부되면(구버전 서버) 스키마 없이 한 번 더 시도하고 그 사실을 note 에 남긴다.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .labels import MAX_CHARS, SCHEMA, build_prompt, parse_json

DEFAULT_URL = "http://127.0.0.1:8097"
N_PREDICT = 120


@dataclass
class Labelled:
    pred: dict
    wall_s: float
    raw: str = ""
    prompt_n: int | None = None
    predicted_n: int | None = None
    pp_tps: float | None = None
    tg_tps: float | None = None
    note: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.pred)


def _post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(url.rstrip("/") + "/completion",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def label_text(text: str, model: str = "minicpm5", url: str = DEFAULT_URL,
               max_chars: int = MAX_CHARS, timeout: int = 900) -> Labelled:
    prompt = build_prompt(text, model, max_chars)
    body = {"prompt": prompt, "n_predict": N_PREDICT, "temperature": 0,
            "cache_prompt": False, "stream": False, "stop": ["<|im_end|>"], "json_schema": SCHEMA}
    t0 = time.time()
    note = ""
    try:
        res = _post(url, body, timeout)
    except urllib.error.HTTPError as e:
        note = f"스키마 거부(HTTP {e.code}) → 스키마 없이 재시도"
        body.pop("json_schema")
        t0 = time.time()
        try:
            res = _post(url, body, timeout)
        except Exception as e2:
            return Labelled({}, round(time.time() - t0, 2), error=f"{note} / {e2}")
    except Exception as e:
        return Labelled({}, round(time.time() - t0, 2), error=str(e))

    wall = round(time.time() - t0, 2)
    raw = res.get("content", "")
    t = res.get("timings", {})
    return Labelled(pred=parse_json(raw), wall_s=wall, raw=raw[:400],
                    prompt_n=t.get("prompt_n"), predicted_n=t.get("predicted_n"),
                    pp_tps=t.get("prompt_per_second"), tg_tps=t.get("predicted_per_second"), note=note)


def health(url: str = DEFAULT_URL, timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
