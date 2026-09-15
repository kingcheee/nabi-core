#!/usr/bin/env python3
"""나비 라벨링 엔진 v0 — CLI.

    python3 -m engine init   ~/nabi --clients "한빛나루식당,대성정밀공업사"
    python3 -m engine serve  --model minicpm5 --threads 4      # llama-server 띄우는 명령을 알려준다
    python3 -m engine scan   ~/nabi                            # inbox → 확인 큐
    python3 -m engine list   ~/nabi [--held|--ready]
    python3 -m engine show   ~/nabi <id>
    python3 -m engine approve ~/nabi <id> [--client X --doc-type Y --period Z]
    python3 -m engine approve ~/nabi --all-ready
    python3 -m engine reject ~/nabi <id> [--why ...]
    python3 -m engine undo   ~/nabi
    python3 -m engine tree   ~/nabi
    python3 -m engine stats  ~/nabi
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .label import DEFAULT_URL, health
from .pipeline import approve, reject, scan
from .rules import Registry
from .store import Workspace

C = {"held": "\033[33m", "ready": "\033[32m", "approved": "\033[36m", "rejected": "\033[31m",
     "skipped": "\033[90m", "off": "\033[0m", "dim": "\033[90m", "b": "\033[1m"}
MARK = {"held": "보류", "ready": "확인대기", "approved": "이동완료", "rejected": "거절", "skipped": "건너뜀"}


def paint(state: str) -> str:
    return f"{C.get(state,'')}{MARK.get(state, state):<5}{C['off']}"


def line(r: dict) -> str:
    lab = f"{r.get('client') or r.get('pred',{}).get('client','?')} · {r.get('doc_type') or '?'} · {r.get('period') or '?'}"
    tail = ""
    if r.get("reasons"):
        tail = f"  {C['dim']}← {', '.join(r.get('explain') or r['reasons'])}{C['off']}"
    elif r.get("target"):
        tail = f"  {C['dim']}→ {r['target']}{C['off']}"
    return f"  {r['id'][:8]}  {paint(r.get('state','?'))}  {lab:<46}{tail}\n            {C['dim']}{r.get('name','')}{C['off']}"


def cmd_init(a) -> int:
    ws = Workspace(a.root)
    ws.init()
    clients = [c.strip() for c in (a.clients or "").split(",") if c.strip()]
    aliases = {}
    for pair in (a.aliases or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            aliases[k.strip()] = v.strip()
    if clients or aliases:
        reg = ws.read_registry()
        reg.clients = sorted(set(reg.clients) | set(clients))
        reg.aliases |= aliases
        ws.write_registry(reg)
    reg = ws.read_registry()
    print(f"작업공간 {ws.root}")
    print(f"  inbox      {ws.inbox}")
    print(f"  정리됨      {ws.organized}")
    print(f"  거래처 {len(reg.clients)}곳 · 별칭 {len(reg.aliases)}개")
    return 0


def cmd_serve(a) -> int:
    models = {"minicpm5": "MiniCPM5-2B-Q4_K_M.gguf", "qwen": "qwen2.5-1.5b-instruct-q4_k_m.gguf"}
    f = models.get(a.model, a.model)
    print(f"{C['b']}모델 서버를 먼저 띄운다{C['off']} — 무상태 세션이라 서류마다 KV 캐시는 새로 연다.\n")
    print(f"  llama-server -m ~/models/{f} \\\n    --host 127.0.0.1 --port 8097 -c 2048 -ub 128 -t {a.threads} --no-warmup\n")
    print(f"살아 있는지: {'예' if health(a.url) else '아니오'}  ({a.url})")
    return 0


def cmd_scan(a) -> int:
    ws = Workspace(a.root)
    ws.init()
    if not a.offline and not health(a.url):
        print(f"{C['held']}모델 서버가 없다{C['off']} — `python3 -m engine serve` 로 띄우는 명령을 본다. ({a.url})")
        return 2
    offline = None
    if a.offline:
        offline = {r["file"]: {"client": r["client"], "category": r["category"],
                              "doc_type": r["doc_type"], "period": r["period"]["normalized"]}
                   for r in json.loads(Path(a.offline).read_text(encoding="utf-8"))}
        print(f"{C['dim']}offline — 모델을 부르지 않고 {Path(a.offline).name} 의 라벨로 규칙만 시험한다{C['off']}")
    files = [Path(f) for f in a.file] if a.file else None
    n = {"held": 0, "ready": 0, "skipped": 0}
    for r in scan(ws, model=a.model, url=a.url, files=files, skip_seen=not a.rescan, offline=offline):
        n[r.get("state", "held")] = n.get(r.get("state", "held"), 0) + 1
        print(line(r))
        if r.get("error"):
            print(f"            {C['held']}{r['error']}{C['off']}")
    print(f"\n확인대기 {n.get('ready',0)} · 보류 {n.get('held',0)} · 건너뜀 {n.get('skipped',0)}")
    print(f"{C['dim']}승인: python3 -m engine approve {a.root} --all-ready{C['off']}")
    return 0


def cmd_list(a) -> int:
    ws = Workspace(a.root)
    rows = ws.queue()
    if a.held:
        rows = [r for r in rows if r.get("state") == "held"]
    if a.ready:
        rows = [r for r in rows if r.get("state") == "ready"]
    for r in rows:
        print(line(r))
    print(f"\n{len(rows)}건")
    return 0


def cmd_show(a) -> int:
    ws = Workspace(a.root)
    r = ws.get(a.id)
    if r is None:
        print(f"큐에 없다: {a.id}"); return 1
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


def cmd_approve(a) -> int:
    ws = Workspace(a.root)
    ids = a.id or []
    if a.all_ready:
        ids = [r["id"] for r in ws.queue() if r.get("state") == "ready"]
        if not ids:
            print("확인대기가 없다"); return 0
    if not ids:
        print("id 를 주거나 --all-ready 를 써라"); return 1
    fix = {k: v for k, v in (("client", a.client), ("doc_type", a.doc_type),
                             ("period", a.period), ("category", a.category)) if v}
    bad = 0
    for i in ids:
        res = approve(ws, i, fix=fix or None)
        if res.get("ok"):
            extra = f" {C['held']}(같은 이름이 있어 _2 를 붙였다){C['off']}" if res.get("renamed") else ""
            print(f"  {C['approved']}이동{C['off']} {i[:8]} → {res['dst']}{extra}")
        else:
            print(f"  {C['held']}실패{C['off']} {i[:8]}: {res['error']}"); bad += 1
    return 1 if bad else 0


def cmd_reject(a) -> int:
    ws = Workspace(a.root)
    res = reject(ws, a.id, a.why or "")
    print(res)
    return 0 if res.get("ok") else 1


def cmd_undo(a) -> int:
    ws = Workspace(a.root)
    res = ws.undo_last()
    if res is None:
        print("되돌릴 이동이 없다"); return 1
    print(f"되돌렸다: {res['dst']} → {res['restored_to']}")
    return 0


def cmd_tree(a) -> int:
    ws = Workspace(a.root)
    if not ws.organized.exists():
        print("정리된 것이 없다"); return 0
    paths = sorted(p for p in ws.organized.rglob("*"))
    for p in paths:
        rel = p.relative_to(ws.organized)
        depth = len(rel.parts) - 1
        name = rel.parts[-1] + ("/" if p.is_dir() else "")
        print("  " + "  " * depth + name)
    print(f"\n파일 {sum(1 for p in paths if p.is_file())}건")
    return 0


def cmd_stats(a) -> int:
    ws = Workspace(a.root)
    rows = ws.queue()
    by = {}
    for r in rows:
        by[r.get("state", "?")] = by.get(r.get("state", "?"), 0) + 1
    print(f"큐 {len(rows)}건 — " + " · ".join(f"{MARK.get(k,k)} {v}" for k, v in sorted(by.items())))
    held = [r for r in rows if r.get("state") == "held"]
    if held:
        rc = {}
        for r in held:
            for x in r.get("reasons", []):
                rc[x] = rc.get(x, 0) + 1
        print("보류 사유: " + " · ".join(f"{k} {v}" for k, v in sorted(rc.items(), key=lambda t: -t[1])))
    labelled = [r for r in rows if r.get("label_s")]
    if labelled:
        avg = sum(r["label_s"] for r in labelled) / len(labelled)
        print(f"모델 호출 {len(labelled)}건 · 건당 평균 {avg:.1f}초")
    mv = [m for m in ws.moves() if not m.get("undone")]
    print(f"이동 {len(mv)}건 (되돌릴 수 있다)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m engine", description="나비 라벨링 엔진 v0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, root=True):
        p = sub.add_parser(name)
        if root:
            p.add_argument("root", nargs="?", default="~/nabi")
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init)
    p.add_argument("--clients", help="쉼표로 구분한 거래처 상호")
    p.add_argument("--aliases", help="약칭=정식상호, 쉼표로 구분")

    p = sub.add_parser("serve")
    p.set_defaults(fn=cmd_serve)
    p.add_argument("--model", default="minicpm5")
    p.add_argument("--threads", default="4")
    p.add_argument("--url", default=DEFAULT_URL)

    p = add("scan", cmd_scan)
    p.add_argument("--model", default="minicpm5")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--file", action="append", help="inbox 대신 이 파일만")
    p.add_argument("--rescan", action="store_true", help="같은 내용이 큐에 있어도 다시 본다")
    p.add_argument("--offline", help="labels.json 경로 — 모델 없이 규칙만 시험한다")

    p = add("list", cmd_list)
    p.add_argument("--held", action="store_true")
    p.add_argument("--ready", action="store_true")

    p = add("show", cmd_show)
    p.add_argument("id")

    p = add("approve", cmd_approve)
    p.add_argument("id", nargs="*")
    p.add_argument("--all-ready", action="store_true")
    p.add_argument("--client")
    p.add_argument("--doc-type", dest="doc_type")
    p.add_argument("--period")
    p.add_argument("--category")

    p = add("reject", cmd_reject)
    p.add_argument("id")
    p.add_argument("--why")

    add("undo", cmd_undo)
    add("tree", cmd_tree)
    add("stats", cmd_stats)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
