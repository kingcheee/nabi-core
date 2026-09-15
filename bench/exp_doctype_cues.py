#!/usr/bin/env python3
"""실험 — 서류 종류를 가르는 단서를 프롬프트에 주면 doc_type 이 오르나.

2026-09-10 정확도 실측에서 doc_type 이 12/22(55%)로 가장 약했다. 프롬프트는 15종을 **이름만 나열**하고
가르는 단서를 주지 않는다. 그래서 모델이 본문의 「사업자등록번호」에 낚여 `사업자등록증` 을 고르고,
`거래명세서`·`카드매출매입내역`·`통장내역` 을 섞는다.

⚠ **방법론 주의 — 이 실험은 과대추정될 수 있다.**
   단서를 시험 대상과 같은 22건에서 뽑으면 시험지를 보고 답을 만드는 것이다. 그래서 단서는
   **국세청·공단이 쓰는 서식 이름**만 썼다(우리 샘플 본문을 읽고 고른 것이 아니다). 그래도 우리 샘플이
   그 서식을 흉내 낸 것이므로 겹침이 남는다. **9/13 샘플 100건으로 다시 재기 전에는 확정값으로 쓰지 마라.**

    python3 bench/exp_doctype_cues.py
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
from engine.labels import CATEGORIES, DOC_TYPES, SCHEMA, parse_json

# 서류 종류 → 그 서류의 정식 서식 이름·표제. 실무 약칭과 서식 이름이 다른 것이 오분류의 원인이다.
CUES = {
    "사업자등록증": "표제가 「사업자등록증」",
    "임대차계약서": "임대인·임차인·보증금·차임이 나오는 계약서",
    "법인등기부등본": "표제가 「등기사항전부증명서」",
    "통장내역": "은행 계좌의 입금·출금·잔액이 날짜순으로 나열된 거래내역",
    "급여대장": "직원별 지급총액·공제액·실지급액 표",
    "근로계약서": "근로시간·임금·계약기간이 나오는 계약서",
    "전자세금계산서": "국세청 승인번호가 있는 세금계산서",
    "종이세금계산서": "승인번호 없이 손으로 쓴 세금계산서",
    "종이영수증": "간이영수증·지출 영수증",
    "4대보험취득확인서": "표제가 「자격취득 확인서」 — 국민연금·건강보험·고용보험·산재보험",
    "거래명세서": "품목·수량·단가·공급가액이 줄줄이 있는 명세서",
    "카드매출매입내역": "카드 승인 건별 매출·매입 내역",
    "잔액증명서": "표제가 「잔액증명서」 — 기준일의 예금 잔액",
    "원천세신고서": "표제가 「원천징수이행상황신고서」",
    "부가세신고서": "표제가 「부가가치세 신고서」 — 과세기간·납부세액",
}

HEAD_A = ("아래는 세무·회계 사무소에 들어온 서류의 본문입니다. 다음 네 가지를 찾아 JSON 객체 하나만 출력하세요. 설명은 쓰지 마세요.\n"
          "- client: 이 서류의 주인인 고객사 상호. 세무대리인·은행·공급자·임대인이 아니라 신고인·예금주·사업장·공급받는자·임차인 쪽.\n")
CAT_LINE = ("- category: 사업자등록증·임대차계약서·법인등기부등본 → 기본서류 / 통장내역·급여대장·근로계약서·전자세금계산서·"
            "종이세금계산서·종이영수증·4대보험취득확인서 → 정기증빙 / 거래명세서·카드매출매입내역·잔액증명서·원천세신고서·부가세신고서 → 신고증빙\n")
PERIOD_LINE = ("- period: 기본서류는 PERMANENT. 월 단위 서류(정기증빙·원천세신고서)는 YYYY-MM. 거래명세서·카드매출매입내역은 분기 YYYY-Q1~Q4. "
               "부가세신고서는 반기 YYYY-1H 또는 YYYY-2H. 잔액증명서는 발급기준일의 연도 YYYY.\n")
EX = '출력 예: {"client": "○○상사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-03"}\n'
TAIL = "\n[서류 본문]\n"

FLAT = "- doc_type: 다음 중 하나: " + ", ".join(DOC_TYPES) + "\n"
CUED = "- doc_type: 다음 중 하나를 고르세요. 괄호는 그 서류를 가르는 단서입니다.\n" + \
       "".join(f"  · {k} ({v})\n" for k, v in CUES.items())

VARIANTS = {
    "flat": HEAD_A + FLAT + CAT_LINE + PERIOD_LINE + EX + TAIL,
    "cued": HEAD_A + CUED + CAT_LINE + PERIOD_LINE + EX + TAIL,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--model", default="minicpm5")
    a = ap.parse_args()
    if not health(a.url):
        print(f"모델 서버가 없다: {a.url}"); return 2
    assert set(CUES) == set(DOC_TYPES), "단서표가 어휘를 다 덮지 않는다"

    rows = json.loads((ROOT / "samples" / "labels.json").read_text(encoding="utf-8"))
    exd = {r["id"]: extract(ROOT / "samples" / "inbox" / r["file"]) for r in rows}

    res = {}
    for name, instr in VARIANTS.items():
        hit = ocr_hit = ocr_n = 0
        conf = Counter()
        toks = []
        t0 = time.time()
        for row in rows:
            p = "<|im_start|>user\n" + instr + exd[row["id"]].text[:3500].strip() + "\n<|im_end|>\n<|im_start|>assistant\n"
            if a.model.startswith("minicpm"):
                p += "<think>\n\n</think>\n\n"
            r = _post(a.url, {"prompt": p, "n_predict": 120, "temperature": 0, "cache_prompt": False,
                              "stream": False, "stop": ["<|im_end|>"], "json_schema": SCHEMA}, 900)
            got = (parse_json(r.get("content", "")).get("doc_type") or "").strip()
            ok = got == row["doc_type"]
            hit += ok
            toks.append((r.get("timings", {}) or {}).get("prompt_n") or 0)
            if exd[row["id"]].scanned:
                ocr_n += 1; ocr_hit += ok
            if not ok:
                conf[f"{row['doc_type']} → {got or '(없음)'}"] += 1
        res[name] = {"hit": hit, "n": len(rows), "ocr_hit": ocr_hit, "ocr_n": ocr_n,
                     "wall_s": round(time.time() - t0, 1), "conf": conf,
                     "prompt_tok": round(sum(toks) / max(1, len(toks)))}
        print(f"{name:6s} doc_type {hit}/{len(rows)} ({100*hit/len(rows):.0f}%) · OCR {ocr_hit}/{ocr_n} · "
              f"프롬프트 평균 {res[name]['prompt_tok']}토큰 · {res[name]['wall_s']}s")
        for k, v in conf.most_common(5):
            print(f"        틀림 {v}회  {k}")

    md = [f"# 실험 — 서류 종류 단서를 프롬프트에 주면 오르나 ({a.model} · {time.strftime('%Y-%m-%d %H:%M')})", "",
          "서류 22건, doc_type 축만 본다.", "",
          "> ⚠ **과대추정 주의.** 단서를 국세청·공단 서식 이름에서만 뽑았지만, 우리 샘플이 그 서식을 흉내 낸",
          "> 것이라 겹침이 남는다. **9/13 샘플 100건으로 다시 재기 전에는 확정값으로 쓰지 마라.**", "",
          "| 조건 | doc_type | OCR 경유 | 프롬프트 길이 | 걸린 시간 |", "|---|---:|---:|---:|---:|"]
    for name, r in res.items():
        md.append(f"| {name} | {r['hit']}/{r['n']} ({100*r['hit']/r['n']:.0f}%) | {r['ocr_hit']}/{r['ocr_n']} | "
                  f"{r['prompt_tok']}토큰 | {r['wall_s']}초 |")
    md += ["", "| 조건 | 무엇 |", "|---|---|",
           "| flat | 15종을 **이름만** 나열 (정본과 같은 방식, 예시 상호만 `○○상사`) |",
           "| cued | 종류마다 **가르는 단서**를 한 줄씩 붙인다 |", ""]
    for name, r in res.items():
        if r["conf"]:
            md += [f"### {name} 에서 틀린 것", "", "| 정답 → 모델 | 횟수 |", "|---|---:|"]
            md += [f"| {k} | {v} |" for k, v in r["conf"].most_common()]
            md.append("")
    md += ["## 프롬프트가 길어지는 대가", "",
           "단서를 붙이면 프롬프트가 길어져 건당 시간이 늘어난다. 위 표의 「프롬프트 길이」와 「걸린 시간」이",
           "그 대가다. 폐폰처럼 pp 가 10 tok/s 대인 기기에서는 토큰 200개가 20초다 — 정확도가 오르는 폭과",
           "견줘 판단해야 한다.", ""]
    outp = ROOT / "bench" / "results" / f"실험-서류종류단서-{time.strftime('%Y%m%d-%H%M')}.md"
    outp.write_text("\n".join(md), encoding="utf-8")
    print("\n" + "\n".join(md)); print(f"결과 {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
