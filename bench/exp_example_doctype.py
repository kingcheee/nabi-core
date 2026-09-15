#!/usr/bin/env python3
"""실험 — 프롬프트 출력 예시의 `doc_type` 값이 그 종류로 끌어당기나.

2026-09-10 실측에서 두 모델의 지배적 오분류가 이렇게 갈렸다:

- **Qwen**: 확정 오탐 5건 중 **4건이 `급여대장`** (d05 통장내역·d09·d10 근로계약서·d13 거래명세서 → 급여대장).
  `급여대장` 은 **프롬프트 출력 예시의 `doc_type` 값**이다.
- **MiniCPM5**: `사업자등록증` 쏠림이 1위, `급여대장` 이 2위(enum 실험 base 조건: 사업자등록증 6 · 급여대장 4).

거래처 쪽에서 「예시를 그대로 베낀다」가 확인됐으니(`exp_prompt_example.py`) 종류 쪽에도 같은 일이
일어나는지 본다. 예시의 client 는 이미 `○○상사` 로 고쳤고, 이번엔 **doc_type 만** 건드린다.

    python3 bench/exp_example_doctype.py [--model minicpm5|qwen]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.extract import extract
from engine.label import DEFAULT_URL, _post, health
from engine.labels import DOC_TYPES, INSTRUCTION, SCHEMA, parse_json

EX_LINE = '출력 예: {"client": "○○상사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-03"}\n'
assert EX_LINE in INSTRUCTION, "정본 프롬프트의 예시 줄이 바뀌었다 — 이 실험을 고쳐라"

VARIANTS = {
    # 정본 그대로 — 예시 doc_type 이 `급여대장`
    "keep": INSTRUCTION,
    # 예시의 doc_type 을 어휘 밖의 자리표시자로 (형식은 보이고 값은 안 끌린다)
    "placeholder": INSTRUCTION.replace('"doc_type": "급여대장"', '"doc_type": "<위 목록 중 하나>"'),
    # 예시 줄을 아예 뺀다 (형식은 json_schema 가 강제한다)
    "no_example": INSTRUCTION.replace(EX_LINE, ""),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--model", default="minicpm5")
    a = ap.parse_args()
    if not health(a.url):
        print(f"모델 서버가 없다: {a.url}"); return 2

    rows = json.loads((ROOT / "samples" / "labels.json").read_text(encoding="utf-8"))
    exd = {r["id"]: extract(ROOT / "samples" / "inbox" / r["file"]) for r in rows}

    res = {}
    for name, instr in VARIANTS.items():
        hit = ocr_hit = ocr_n = 0
        picked = Counter()
        wrong_to = Counter()
        t0 = time.time()
        for row in rows:
            p = "<|im_start|>user\n" + instr + exd[row["id"]].text[:3500].strip() + "\n<|im_end|>\n<|im_start|>assistant\n"
            if a.model.startswith("minicpm"):
                p += "<think>\n\n</think>\n\n"
            r = _post(a.url, {"prompt": p, "n_predict": 120, "temperature": 0, "cache_prompt": False,
                              "stream": False, "stop": ["<|im_end|>"], "json_schema": SCHEMA}, 900)
            got = (parse_json(r.get("content", "")).get("doc_type") or "").strip()
            picked[got] += 1
            ok = got == row["doc_type"]
            hit += ok
            if exd[row["id"]].scanned:
                ocr_n += 1; ocr_hit += ok
            if not ok:
                wrong_to[got or "(없음)"] += 1
        res[name] = {"hit": hit, "n": len(rows), "ocr_hit": ocr_hit, "ocr_n": ocr_n,
                     "wall_s": round(time.time() - t0, 1), "wrong_to": wrong_to,
                     "example_val": picked.get("급여대장", 0)}
        print(f"{name:12s} doc_type {hit}/{len(rows)} ({100*hit/len(rows):.0f}%) · OCR {ocr_hit}/{ocr_n} · "
              f"`급여대장` 을 고른 횟수 {picked.get('급여대장',0)} · {res[name]['wall_s']}s")
        print(f"             틀렸을 때 고른 값: {', '.join(f'{k} {v}' for k,v in wrong_to.most_common(4))}")

    n_gt = sum(1 for r in rows if r["doc_type"] == "급여대장")
    md = [f"# 실험 — 출력 예시의 `doc_type` 이 그 종류로 끌어당기나 ({a.model} · {time.strftime('%Y-%m-%d %H:%M')})", "",
          f"서류 22건, doc_type 축만 본다. 22건 중 실제 `급여대장` 은 **{n_gt}건**이다 — 그보다 많이 고르면 끌린 것이다.", "",
          "| 조건 | doc_type | OCR 경유 | `급여대장` 을 고른 횟수 | 틀렸을 때 고른 값 |", "|---|---:|---:|---:|---|"]
    for name, r in res.items():
        wt = " · ".join(f"`{k}` {v}" for k, v in r["wrong_to"].most_common(3))
        md.append(f"| {name} | {r['hit']}/{r['n']} ({100*r['hit']/r['n']:.0f}%) | {r['ocr_hit']}/{r['ocr_n']} | "
                  f"**{r['example_val']}** (실제 {n_gt}건) | {wt} |")
    md += ["", "| 조건 | 무엇 |", "|---|---|",
           "| keep | 정본 그대로 — 예시 `doc_type` 이 `급여대장` |",
           "| placeholder | 예시의 `doc_type` 을 `<위 목록 중 하나>` 로 (형식은 보이고 값은 안 보인다) |",
           "| no_example | 출력 예시 줄을 아예 뺀다 |", ""]
    outp = ROOT / "bench" / "results" / f"실험-예시서류종류-{a.model}-{time.strftime('%Y%m%d-%H%M')}.md"
    outp.write_text("\n".join(md), encoding="utf-8")
    print("\n" + "\n".join(md)); print(f"결과 {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
