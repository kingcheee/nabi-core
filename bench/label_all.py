#!/usr/bin/env python3
"""엔진 정확도 실측 — 샘플 전건을 파서→모델→규칙으로 돌려 정답 라벨과 대조한다.

    python3 bench/label_all.py [--model minicpm5] [--url http://127.0.0.1:8097] [--out bench/results/...]

재는 것:
  ① 축별 정확도 4개(거래처·분류·서류종류·기간). 텍스트 원본과 OCR 경유를 나눠 낸다(기획서 §5.3 두 줄).
  ② 보류율, 그리고 **자신있는 오탐** — 「확정」으로 내놨는데 틀린 비율. 이 수가 낮아야 확인 큐가 의미가 있다.
  ③ 목표 경로 일치율 — 라벨이 다 맞아도 경로가 맞아야 실제로 정리가 된 것이다.

정답은 `samples/labels.json`. 모델은 llama-server 에 상주시켜 둔다(`python3 -m engine serve` 참고).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.extract import extract
from engine.label import DEFAULT_URL, health, label_text
from engine.labels import norm_client, norm_period, target_path
from engine.rules import Registry, decide

AXES = ("client", "category", "doc_type", "period")


def client_ok(pred_client: str, row: dict) -> bool:
    """정답의 정식 상호 또는 약칭에 붙으면 맞다고 본다(label_one.py 와 같은 기준)."""
    pc = norm_client(pred_client or "")
    ec, es = norm_client(row["client"]), norm_client(row.get("client_short", ""))
    return bool(pc) and (pc == ec or (es and pc == es) or (es and es in pc) or (len(pc) >= 3 and pc in ec))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="minicpm5")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out")
    ap.add_argument("--only", help="쉼표로 구분한 id — 일부만")
    a = ap.parse_args()

    if not health(a.url):
        print(f"모델 서버가 없다: {a.url}"); return 2

    rows = json.loads((ROOT / "samples" / "labels.json").read_text(encoding="utf-8"))
    if a.only:
        keep = {x.strip() for x in a.only.split(",")}
        rows = [r for r in rows if r["id"] in keep]

    clients = sorted({r["client"] for r in rows})
    aliases = {r["client_short"]: r["client"] for r in rows if r.get("client_short")}
    reg = Registry(clients=clients, aliases=aliases)

    out: list[dict] = []
    print(f"{'id':4s} {'서류':16s} {'경로':4s} " + " ".join(f"{x:8s}" for x in AXES) + " 판정   초")
    for row in rows:
        p = ROOT / "samples" / "inbox" / row["file"]
        t0 = time.time()
        ex = extract(p)
        lab = label_text(ex.text, model=a.model, url=a.url)
        v = decide(lab.pred, reg, text=ex.text, scanned=ex.scanned, ext=ex.ext)

        want = {"client": row["client"], "category": row["category"], "doc_type": row["doc_type"],
                "period": row["period"]["normalized"]}
        hit = {
            "client": client_ok(lab.pred.get("client", ""), row),      # 모델이 낸 상호가 그대로 맞았나
            "category": v.category == want["category"],          # 규칙이 파생한 값으로 잰다
            "doc_type": (lab.pred.get("doc_type") or "") == want["doc_type"],
            "period": norm_period(lab.pred.get("period", "")) == want["period"],
        }
        all_ok = all(hit.values())
        # 규칙이 붙인 상호 — 근사 매칭이 모델의 오타를 고쳐 붙였는지를 따로 잰다(ADR-0007 규칙엔진의 값).
        client_fixed = v.client == row["client"]
        hit_after = dict(hit) | {"client": client_fixed}
        all_ok_after = all(hit_after.values())
        path_ok = bool(v.target) and v.target == row["target_path"]

        out.append({"id": row["id"], "file": row["file"], "scanned": ex.scanned, "chars": len(ex.text),
                    "pred": lab.pred, "want": want, "hit": hit, "all_ok": all_ok,
                    "hit_after": hit_after, "all_ok_after": all_ok_after, "rule_client": v.client,
                    "held": v.held, "reasons": v.reasons, "target": v.target,
                    "want_target": row["target_path"], "path_ok": path_ok,
                    "wall_s": round(time.time() - t0, 2), "label_s": lab.wall_s,
                    "pp_tps": lab.pp_tps, "tg_tps": lab.tg_tps, "prompt_n": lab.prompt_n,
                    "error": lab.error})
        mark = "".join("O" if hit[x] else "X" for x in AXES)
        print(f"{row['id']:4s} {row['doc_type'][:16]:16s} {'O' if path_ok else 'X':4s} "
              + " ".join(f"{'O' if hit[x] else 'X':8s}" for x in AXES)
              + f" {'보류' if v.held else '확정':4s} {out[-1]['label_s']:5.1f}"
              + ("  " + ",".join(v.reasons) if v.reasons else ""))

    # ---------------- 집계
    def agg(sel: list[dict], name: str) -> list[str]:
        if not sel:
            return []
        n = len(sel)
        lines = [f"### {name} ({n}건)", ""]
        lines.append("| 축 | 모델 원값 | 규칙 통과 후 |")
        lines.append("|---|---:|---:|")
        for x in AXES:
            k = sum(1 for r in sel if r["hit"][x])
            ka = sum(1 for r in sel if r["hit_after"][x])
            suf = "  ←규칙이 목록 대조로 고친다" if x == "client" else ""
            lines.append(f"| {x} | {k}/{n} ({100*k/n:.0f}%) | {ka}/{n} ({100*ka/n:.0f}%){suf} |")
        k4 = sum(1 for r in sel if r["all_ok"])
        k4a = sum(1 for r in sel if r["all_ok_after"])
        kp = sum(1 for r in sel if r["path_ok"])
        lines.append(f"| **4축 전부** | **{k4}/{n} ({100*k4/n:.0f}%)** | **{k4a}/{n} ({100*k4a/n:.0f}%)** |")
        held = [r for r in sel if r["held"]]
        conf = [r for r in sel if not r["held"]]
        # 보류는 경로를 만들지 않는다. 그래서 전체를 분모로 쓰면 지표가 망가진다 — 확정 건만 본다.
        lines.append(f"| 경로 일치 (확정 {len(conf)}건 중) | — | {kp}/{len(conf) or 1} ({100*kp/(len(conf) or 1):.0f}%) |")
        conf_wrong = [r for r in conf if not r["all_ok_after"]]
        held_right = [r for r in held if not r["all_ok_after"]]
        lines += ["", f"- 보류 {len(held)}/{n} ({100*len(held)/n:.0f}%) · 확정 {len(conf)}건",
                  f"- **자신있는 오탐 {len(conf_wrong)}/{len(conf) or 1} ({100*len(conf_wrong)/(len(conf) or 1):.0f}%)** — 확정으로 내놨는데 틀린 것",
                  f"- 보류가 옳았던 비율 {len(held_right)}/{len(held) or 1} ({100*len(held_right)/(len(held) or 1):.0f}%) — 보류한 것 중 실제로 틀렸던 것"]
        secs = [r["label_s"] for r in sel if r["label_s"]]
        if secs:
            lines.append(f"- 건당 모델 시간 평균 {sum(secs)/len(secs):.1f}초 (최소 {min(secs):.1f} · 최대 {max(secs):.1f})")
        return lines + [""]

    txt = [r for r in out if not r["scanned"]]
    ocr = [r for r in out if r["scanned"]]
    md = [f"# 엔진 정확도 실측 — {a.model} · {time.strftime('%Y-%m-%d %H:%M')}", "",
          f"- 서류 {len(out)}건(텍스트 {len(txt)} · OCR 경유 {len(ocr)}) · 모델 {a.model} · 4축 라벨",
          "- 파서→모델(json_schema 강제)→규칙엔진. 정답은 `samples/labels.json`.", ""]
    md += agg(out, "전체") + agg(txt, "텍스트 원본") + agg(ocr, "스캔·팩스·카톡 사진 (OCR 경유)")

    wrong = [r for r in out if not r["all_ok_after"]]
    if wrong:
        md += ["### 규칙을 통과한 뒤에도 틀린 건", "",
               "| id | 서류 | 축 | 모델이 낸 값 | 정답 | 판정 |", "|---|---|---|---|---|---|"]
        for r in wrong:
            for x in AXES:
                if not r["hit_after"][x]:
                    md.append(f"| {r['id']} | {r['file']} | {x} | `{r['pred'].get(x,'')}` | `{r['want'][x]}` | "
                              f"{'보류' if r['held'] else '**확정(오탐)**'} |")
        md.append("")

    fixed = [r for r in out if not r["all_ok"] and r["all_ok_after"]]
    if fixed:
        md += ["### 규칙엔진이 고친 건 (ADR-0007 이 실제로 값을 낸 자리)", "",
               "| id | 서류 | 모델이 낸 상호 | 규칙이 붙인 상호 |", "|---|---|---|---|"]
        for r in fixed:
            md.append(f"| {r['id']} | {r['file']} | `{r['pred'].get('client','')}` | `{r['rule_client']}` |")
        md.append("")

    body = "\n".join(md)
    print("\n" + body)
    stamp = time.strftime("%Y%m%d-%H%M")
    outp = Path(a.out) if a.out else ROOT / "bench" / "results" / f"정확도-{a.model}-{stamp}.md"
    outp.write_text(body, encoding="utf-8")
    outp.with_suffix(".jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out), encoding="utf-8")
    print(f"\n결과 {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
