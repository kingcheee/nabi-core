#!/usr/bin/env python3
"""실험 — 모델이 확신 없을 때 doc_type enum 의 **첫 항목**을 고르는지 본다.

2026-09-10 정확도 실측에서 doc_type 이 15/22 축 중 가장 약했고(55%), 틀린 예측이 `사업자등록증` 으로
심하게 몰렸다. `사업자등록증` 은 `DOC_TYPES[0]` 이다. 순서가 원인이면 프롬프트 한 줄로 고칠 수 있다.

세 조건을 같은 22건에 돌린다(정본 프롬프트를 건드리지 않고 여기서만 변형한다):
  base    — 정본 순서
  rotate  — 사업자등록증을 맨 뒤로
  reverse — 순서를 뒤집는다

    python3 bench/exp_enum_order.py [--url ...] [--model minicpm5]
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
from engine.labels import CATEGORIES, DOC_TYPES, parse_json


def instruction_for(types: list[str]) -> str:
    return (
        "아래는 세무·회계 사무소에 들어온 서류의 본문입니다. 다음 네 가지를 찾아 JSON 객체 하나만 출력하세요. 설명은 쓰지 마세요.\n"
        "- client: 이 서류의 주인인 고객사 상호. 세무대리인·은행·공급자·임대인이 아니라 신고인·예금주·사업장·공급받는자·임차인 쪽.\n"
        "- doc_type: 다음 중 하나: " + ", ".join(types) + "\n"
        "- category: 사업자등록증·임대차계약서·법인등기부등본 → 기본서류 / 통장내역·급여대장·근로계약서·전자세금계산서·종이세금계산서·종이영수증·4대보험취득확인서 → 정기증빙 / 거래명세서·카드매출매입내역·잔액증명서·원천세신고서·부가세신고서 → 신고증빙\n"
        "- period: 기본서류는 PERMANENT. 월 단위 서류(정기증빙·원천세신고서)는 YYYY-MM. 거래명세서·카드매출매입내역은 분기 YYYY-Q1~Q4. "
        "부가세신고서는 반기 YYYY-1H 또는 YYYY-2H. 잔액증명서는 발급기준일의 연도 YYYY.\n"
        "출력 예: {\"client\": \"가상상사\", \"category\": \"정기증빙\", \"doc_type\": \"급여대장\", \"period\": \"2026-03\"}\n\n[서류 본문]\n"
    )


def run(cond: str, types: list[str], texts: dict, rows: list[dict], url: str, model: str) -> dict:
    schema = {"type": "object",
              "properties": {"client": {"type": "string"},
                             "category": {"type": "string", "enum": CATEGORIES},
                             "doc_type": {"type": "string", "enum": types},
                             "period": {"type": "string"}},
              "required": ["client", "category", "doc_type", "period"]}
    instr = instruction_for(types)
    hit = 0
    preds = Counter()
    t0 = time.time()
    for row in rows:
        p = "<|im_start|>user\n" + instr + texts[row["id"]][:3500].strip() + "\n<|im_end|>\n<|im_start|>assistant\n"
        if model.startswith("minicpm"):
            p += "<think>\n\n</think>\n\n"
        res = _post(url, {"prompt": p, "n_predict": 120, "temperature": 0, "cache_prompt": False,
                          "stream": False, "stop": ["<|im_end|>"], "json_schema": schema}, 900)
        got = (parse_json(res.get("content", "")).get("doc_type") or "").strip()
        preds[got] += 1
        if got == row["doc_type"]:
            hit += 1
    return {"cond": cond, "first": types[0], "hit": hit, "n": len(rows),
            "wall_s": round(time.time() - t0, 1), "preds": preds}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--model", default="minicpm5")
    a = ap.parse_args()
    if not health(a.url):
        print(f"모델 서버가 없다: {a.url}"); return 2

    rows = json.loads((ROOT / "samples" / "labels.json").read_text(encoding="utf-8"))
    texts = {r["id"]: extract(ROOT / "samples" / "inbox" / r["file"]).text for r in rows}

    rotated = DOC_TYPES[1:] + DOC_TYPES[:1]
    conds = [("base", list(DOC_TYPES)), ("rotate", rotated), ("reverse", list(reversed(DOC_TYPES)))]

    out = []
    for name, types in conds:
        r = run(name, types, texts, rows, a.url, a.model)
        out.append(r)
        top = ", ".join(f"{k or '(없음)'} {v}" for k, v in r["preds"].most_common(4))
        print(f"{name:8s} 첫항목={r['first']:12s} doc_type {r['hit']}/{r['n']} "
              f"({100*r['hit']/r['n']:.0f}%) · {r['wall_s']}s · 많이 고른 것: {top}")

    md = [f"# 실험 — doc_type enum 순서가 정확도를 바꾸나 ({a.model} · {time.strftime('%Y-%m-%d %H:%M')})", "",
          "서류 22건, doc_type 축만 본다. 정본 프롬프트는 건드리지 않고 이 스크립트에서만 순서를 바꿨다.", "",
          "| 조건 | enum 첫 항목 | doc_type 정확도 | 가장 많이 고른 값 |", "|---|---|---:|---|"]
    for r in out:
        top = " · ".join(f"`{k or '(없음)'}` {v}" for k, v in r["preds"].most_common(3))
        md.append(f"| {r['cond']} | `{r['first']}` | {r['hit']}/{r['n']} ({100*r['hit']/r['n']:.0f}%) | {top} |")
    md += ["", "## 읽는 법", "",
           "조건 사이에 정확도 차이가 크고 **가장 많이 고른 값이 매번 첫 항목으로 따라 움직이면** 순서 편향이다.",
           "차이가 작고 많이 고른 값이 그대로면 순서는 원인이 아니고, doc_type 을 가르는 단서를 프롬프트에 더 줘야 한다.", ""]
    outp = ROOT / "bench" / "results" / f"실험-enum순서-{time.strftime('%Y%m%d-%H%M')}.md"
    outp.write_text("\n".join(md), encoding="utf-8")
    print("\n" + "\n".join(md))
    print(f"결과 {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
