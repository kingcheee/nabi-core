#!/usr/bin/env python3
"""실험 — 프롬프트의 출력 예시가 거래처 인식을 망치나.

2026-09-10 정확도 실측에서 OCR 경유 서류의 client 가 2/8 이었고, 틀린 값이 `가상상사`·`가상종합상사`·
`흥가상`·`홍가상` 처럼 **「가상」을 낀 조합**으로 몰렸다. 원인 후보 둘이 겹쳐 있다:

  ① 프롬프트의 출력 예시가 `"client": "가상상사"` 다.
  ② 샘플 서류 본문에 `가상카드`·`가상주유소`·`가상세무회계사무소`·`가상은행`·`가상로` 가 도배돼 있다
     (가짜 데이터임을 드러내려고 붙인 접두어인데, 하필 예시와 겹친다).

즉 모델이 상호를 못 찾으면 예시와 본문에서 「가상」을 주워 상호를 지어낸다. 예시만 바꿔 갈라 본다.

    python3 bench/exp_prompt_example.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.extract import extract
from engine.label import DEFAULT_URL, _post, health
from engine.labels import CATEGORIES, DOC_TYPES, SCHEMA, norm_client, parse_json
from engine.rules import Registry

HEAD = (
    "아래는 세무·회계 사무소에 들어온 서류의 본문입니다. 다음 네 가지를 찾아 JSON 객체 하나만 출력하세요. 설명은 쓰지 마세요.\n"
    "- client: 이 서류의 주인인 고객사 상호. 세무대리인·은행·공급자·임대인이 아니라 신고인·예금주·사업장·공급받는자·임차인 쪽.\n"
    "- doc_type: 다음 중 하나: " + ", ".join(DOC_TYPES) + "\n"
    "- category: 사업자등록증·임대차계약서·법인등기부등본 → 기본서류 / 통장내역·급여대장·근로계약서·전자세금계산서·종이세금계산서·종이영수증·4대보험취득확인서 → 정기증빙 / 거래명세서·카드매출매입내역·잔액증명서·원천세신고서·부가세신고서 → 신고증빙\n"
    "- period: 기본서류는 PERMANENT. 월 단위 서류(정기증빙·원천세신고서)는 YYYY-MM. 거래명세서·카드매출매입내역은 분기 YYYY-Q1~Q4. "
    "부가세신고서는 반기 YYYY-1H 또는 YYYY-2H. 잔액증명서는 발급기준일의 연도 YYYY.\n"
)
TAIL = "\n[서류 본문]\n"

VARIANTS = {
    "base": HEAD + '출력 예: {"client": "가상상사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-03"}\n' + TAIL,
    "placeholder": HEAD + '출력 예: {"client": "○○상사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-03"}\n' + TAIL,
    "no_example": HEAD + TAIL,
    "warn": HEAD + '출력 예: {"client": "○○상사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-03"}\n'
                 + "client 는 반드시 본문에 그대로 적힌 상호를 옮겨 적으세요. 본문에 없는 이름을 만들지 마세요.\n" + TAIL,
}


def client_ok(pred: str, row: dict) -> bool:
    pc = norm_client(pred or "")
    ec, es = norm_client(row["client"]), norm_client(row.get("client_short", ""))
    return bool(pc) and (pc == ec or (es and pc == es) or (es and es in pc) or (len(pc) >= 3 and pc in ec))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--model", default="minicpm5")
    a = ap.parse_args()
    if not health(a.url):
        print(f"모델 서버가 없다: {a.url}"); return 2

    rows = json.loads((ROOT / "samples" / "labels.json").read_text(encoding="utf-8"))
    exd = {r["id"]: extract(ROOT / "samples" / "inbox" / r["file"]) for r in rows}
    reg = Registry(clients=sorted({r["client"] for r in rows}),
                   aliases={r["client_short"]: r["client"] for r in rows if r.get("client_short")})

    res = {}
    for name, instr in VARIANTS.items():
        raw = fixed = 0
        made_up = Counter()
        t0 = time.time()
        per_ocr = [0, 0]
        for row in rows:
            p = "<|im_start|>user\n" + instr + exd[row["id"]].text[:3500].strip() + "\n<|im_end|>\n<|im_start|>assistant\n"
            if a.model.startswith("minicpm"):
                p += "<think>\n\n</think>\n\n"
            r = _post(a.url, {"prompt": p, "n_predict": 120, "temperature": 0, "cache_prompt": False,
                              "stream": False, "stop": ["<|im_end|>"], "json_schema": SCHEMA}, 900)
            got = (parse_json(r.get("content", "")).get("client") or "").strip()
            ok = client_ok(got, row)
            raw += ok
            m = reg.match_client(got)
            fx = m.name == row["client"]
            fixed += fx
            if exd[row["id"]].scanned:
                per_ocr[0] += 1; per_ocr[1] += fx
            if not ok and got:
                # 본문에 없는 이름을 지어냈나
                if norm_client(got) not in norm_client(exd[row["id"]].text):
                    made_up[got] += 1
        res[name] = {"raw": raw, "fixed": fixed, "n": len(rows), "wall_s": round(time.time() - t0, 1),
                     "made_up": made_up, "ocr_n": per_ocr[0], "ocr_fixed": per_ocr[1]}
        print(f"{name:12s} 모델원값 {raw}/{len(rows)} ({100*raw/len(rows):.0f}%) · "
              f"규칙후 {fixed}/{len(rows)} ({100*fixed/len(rows):.0f}%) · "
              f"OCR {per_ocr[1]}/{per_ocr[0]} · 지어낸 이름 {sum(made_up.values())}개 "
              f"{', '.join(k for k,_ in made_up.most_common(3))} · {res[name]['wall_s']}s")

    md = [f"# 실험 — 프롬프트 출력 예시가 거래처 인식을 망치나 ({a.model} · {time.strftime('%Y-%m-%d %H:%M')})", "",
          "서류 22건(OCR 경유 8건 포함), client 축만 본다.", "",
          "| 조건 | 모델 원값 | 규칙 통과 후 | OCR 경유 | 본문에 없는 이름을 지어낸 횟수 |",
          "|---|---:|---:|---:|---|"]
    for name, r in res.items():
        mu = ", ".join(f"`{k}`" for k, _ in r["made_up"].most_common(3)) or "—"
        md.append(f"| {name} | {r['raw']}/{r['n']} ({100*r['raw']/r['n']:.0f}%) | "
                  f"{r['fixed']}/{r['n']} ({100*r['fixed']/r['n']:.0f}%) | {r['ocr_fixed']}/{r['ocr_n']} | "
                  f"{sum(r['made_up'].values())}회 — {mu} |")
    md += ["", "## 조건", "",
           "| 이름 | 무엇을 바꿨나 |", "|---|---|",
           "| base | 정본 그대로 — 출력 예시의 client 가 `가상상사` |",
           "| placeholder | 예시 client 를 `○○상사` 로 (샘플 본문의 「가상」과 겹치지 않게) |",
           "| no_example | 출력 예시 줄을 아예 뺀다 (형식은 json_schema 가 이미 강제한다) |",
           "| warn | placeholder + 「본문에 그대로 적힌 상호를 옮겨 적어라」 한 줄 추가 |", ""]
    outp = ROOT / "bench" / "results" / f"실험-프롬프트예시-{time.strftime('%Y%m%d-%H%M')}.md"
    outp.write_text("\n".join(md), encoding="utf-8")
    print("\n" + "\n".join(md)); print(f"결과 {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
