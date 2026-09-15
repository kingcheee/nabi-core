#!/usr/bin/env python3
"""여러 기기의 bench/results/*.jsonl 을 기획서용 비교표 두 개로 합친다.

    python3 bench/merge_results.py            # 마크다운 표를 stdout 으로
    python3 bench/merge_results.py -o OUT.md  # 파일로

- 표 1: 기기 × 모델 × 스레드 → pp2048 / tg64
- 표 2: 기기 × 모델 → 라벨링 건당 초(중앙값·최대) · 프롬프트 토큰 중앙값 · 라벨 정확도
- 표 3(있을 때): 발열 — 시작/최고/끝 온도, 스로틀링 판정
정확도는 summary 레코드의 client_ok·doc_type_ok·period_ok(3축, 기획서 §5.3 성공 기준과 같은 축)를 센다.
summary 가 없는 중단 회차는 label 레코드의 match 딕트로 직접 센다.
표 1 에서 tg 가 pp 의 1/20 밑이면 ⚠스레드설정 을 붙인다 — 리틀코어 지각·taskset 과다구독의 신호다.
"""
import argparse, glob, json, os, statistics as st
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")


def med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(st.median(xs), 1) if xs else None


def load():
    runs = []
    for f in sorted(glob.glob(os.path.join(RES, "*.jsonl"))):
        host = os.path.basename(f).rsplit("-", 2)[0]
        stamp = os.path.basename(f)[len(host) + 1:-6]
        recs = []
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        if recs:
            runs.append((host, stamp, recs))
    return runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    runs = load()
    out = []

    out.append("## 표 1 — 처리 속도 (llama-bench, pp2048 / tg64 tok/s)\n")
    out.append("| 기기 | 모델 | 스레드 | taskset | pp2048 | tg64 | 회차 |")
    out.append("|---|---|---:|---|---:|---:|---|")
    rows1 = []
    for host, stamp, recs in runs:
        for r in recs:
            if r.get("kind") == "bench":
                rows1.append((host, r.get("model"), r.get("threads"), r.get("taskset") or "—",
                              r.get("pp"), r.get("tg"), stamp))
    for h, m, t, ts, pp, tg, s in rows1:
        note = ""
        if pp is None and tg is None:
            note = " ⛔중단"
        elif isinstance(tg, (int, float)) and isinstance(pp, (int, float)) and pp > 0 and tg < pp / 20:
            # tg 가 pp 의 1/20 밑이면 스레드 설정이 잘못된 신호다(리틀코어 지각·과다구독).
            note = " ⚠스레드설정"
        out.append(f"| {h} | {m} | {t} | {ts} | {pp if pp is not None else 'NA'} | "
                   f"{tg if tg is not None else 'NA'} | {s}{note} |")

    out.append("\n## 표 2 — 라벨링 실측 (서류 1건 = 세션 1회)\n")
    out.append("| 기기 | 모델 | 스레드 | 건수 | 건당 초(중앙) | 건당 초(최대) | 프롬프트 tok(중앙) | 정확도 |")
    out.append("|---|---|---:|---:|---:|---:|---:|---|")
    g = defaultdict(list)
    for host, stamp, recs in runs:
        for r in recs:
            if r.get("kind") in ("label", "labeling", "doc"):
                g[(host, r.get("model"), r.get("threads"))].append(r)
    # 정확도는 summary 레코드에 있다(bench.sh 가 3축을 세어 적는다). label 레코드에는 match 딕트가 있다.
    summ = {}
    for host, stamp, recs in runs:
        for r in recs:
            if r.get("kind") == "summary":
                summ[(host, r.get("model"), r.get("threads"))] = r
    for (h, m, t), rs in sorted(g.items()):
        secs = [r.get("wall_s") or r.get("secs") or r.get("elapsed") for r in rs]
        secs = [x for x in secs if isinstance(x, (int, float))]
        toks = [r.get("prompt_n") or r.get("prompt_tokens") or r.get("prompt_tok") for r in rs]
        sm = summ.get((h, m, t))
        if sm:
            n = sm.get("n") or len(rs)
            parts = [f"{k[:-3]} {sm[k]}/{n}" for k in ("client_ok", "doc_type_ok", "period_ok") if k in sm]
            tot3 = sum(sm.get(k, 0) for k in ("client_ok", "doc_type_ok", "period_ok"))
            acc = f"**{tot3}/{n*3}** ({' · '.join(parts)})" if parts else "NA"
        else:
            # summary 가 없으면(중단된 회차) label 레코드의 match 로 직접 센다
            axes = ("client", "doc_type", "period")
            tot = ok = 0
            for r in rs:
                mm = r.get("match") or {}
                for k in axes:
                    if k in mm:
                        tot += 1
                        ok += 1 if mm[k] else 0
            acc = f"{ok}/{tot}" if tot else "NA"
        out.append(f"| {h} | {m} | {t} | {len(rs)} | {med(secs) or 'NA'} | "
                   f"{round(max(secs),1) if secs else 'NA'} | {med(toks) or 'NA'} | {acc} |")

    therm = [(h, r) for h, s, recs in runs for r in recs if r.get("kind") in ("thermal", "temp")]
    if therm:
        out.append("\n## 표 3 — 발열 (10분 연속)\n")
        out.append("| 기기 | 경과 s | 온도 °C | CPU MHz | 건수 |")
        out.append("|---|---:|---:|---:|---:|")
        for h, r in therm:
            out.append(f"| {h} | {r.get('t') or r.get('elapsed')} | {r.get('temp_c') or r.get('temp')} | "
                       f"{r.get('mhz') or '—'} | {r.get('done') or '—'} |")

    text = "\n".join(out) + "\n"
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text)
        print(f"wrote {a.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
