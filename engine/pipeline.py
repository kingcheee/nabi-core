#!/usr/bin/env python3
"""파이프라인 — 파서 → 모델 JSON → 규칙 대조 → 확인 큐 → (승인 뒤) 이동.

기획서 §3의 네 칸을 그대로 코드로 옮긴 것이다. 스캔 단계는 **파일을 만지지 않는다** —
큐에 올릴 뿐이고, 실제 이동은 사람이 승인한 뒤 `approve` 에서 일어난다.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

from .extract import UnsupportedFormat, extract
from .label import DEFAULT_URL, label_text
from .rules import Registry, decide, derive_category
from .store import Workspace, now, sha256


def scan_one(ws: Workspace, path: Path, reg: Registry, model: str, url: str,
             offline_pred: dict | None = None) -> dict:
    """파일 1건을 큐 레코드로 만든다. offline_pred 를 주면 모델을 부르지 않는다(규칙만 시험할 때)."""
    t0 = time.time()
    digest = sha256(path)
    row = {"id": digest[:12], "sha256": digest, "src": str(path), "name": path.name,
           "ts": now(), "model": model, "state": "held"}
    try:
        ex = extract(path)
    except (UnsupportedFormat, Exception) as e:
        return {**row, "state": "held", "reasons": ["parse_failed"],
                "error": f"{type(e).__name__}: {e}", "wall_s": round(time.time() - t0, 2)}

    row |= {"chars": len(ex.text), "scanned": ex.scanned, "ext": ex.ext, "how": ex.how}

    if offline_pred is not None:
        pred, lbl_wall, note, err = offline_pred, 0.0, "offline_pred (모델 미호출)", ""
        pp = tg = ptok = None
    else:
        lab = label_text(ex.text, model=model, url=url)
        pred, lbl_wall, note, err = lab.pred, lab.wall_s, lab.note, lab.error
        pp, tg, ptok = lab.pp_tps, lab.tg_tps, lab.prompt_n
        if err:
            return {**row, "reasons": ["model_failed"], "error": err, "wall_s": round(time.time() - t0, 2)}

    v = decide(pred, reg, text=ex.text, scanned=ex.scanned, ext=ex.ext)
    return {**row,
            "state": "held" if v.held else "ready",
            "pred": v.pred, "client": v.client, "client_score": v.client_score, "client_how": v.client_how,
            "category": v.category, "doc_type": v.doc_type, "period": v.period,
            "reasons": v.reasons, "explain": v.explain(), "target": v.target,
            "label_s": lbl_wall, "wall_s": round(time.time() - t0, 2),
            "prompt_n": ptok, "pp_tps": pp, "tg_tps": tg, "note": note}


def scan(ws: Workspace, model: str = "minicpm5", url: str = DEFAULT_URL,
         files: list[Path] | None = None, skip_seen: bool = True, offline: dict | None = None):
    """inbox(또는 files)를 훑어 큐에 올린다. 같은 내용(sha256)을 다시 넣지 않는다."""
    ws.init()
    reg = ws.read_registry()
    seen = ws.seen_hashes() if skip_seen else set()
    targets = files if files is not None else sorted(p for p in ws.inbox.iterdir() if p.is_file())
    for p in targets:
        if skip_seen and sha256(p) in seen:
            yield {"id": sha256(p)[:12], "name": p.name, "state": "skipped", "note": "같은 내용이 이미 큐에 있다"}
            continue
        pred = None
        if offline is not None:
            pred = offline.get(p.name)
            if pred is None:
                yield {"id": sha256(p)[:12], "name": p.name, "state": "skipped", "note": "offline 라벨 없음"}
                continue
        row = scan_one(ws, p, reg, model, url, offline_pred=pred)
        ws.append_queue(row)
        yield row


def approve(ws: Workspace, doc_id: str, fix: dict | None = None) -> dict:
    """승인 → 이동. fix 를 주면 라벨을 고쳐 다시 판정한 뒤 이동한다(수정은 별칭으로 규칙에 되돌아간다)."""
    row = ws.get(doc_id)
    if row is None:
        return {"ok": False, "error": f"큐에 없다(또는 앞자리가 여럿에 걸린다): {doc_id}"}
    if row.get("state") == "approved":
        return {"ok": False, "error": "이미 승인·이동됐다"}

    if fix:
        pred = {**row.get("pred", {}), **{k: v for k, v in fix.items() if v}}
        # 사람이 서류 종류를 확인·수정하고 승인하는 순간 분류는 서류 종류가 정한다 — 모델이 냈던 분류(category_mismatch)는 더 볼 이유가 없다
        if pred.get("doc_type") and derive_category(pred["doc_type"]):
            pred["category"] = derive_category(pred["doc_type"])
        reg = ws.read_registry()
        ex_text = ""      # 재판정에 본문이 필요한 조건(OCR)은 이미 스캔 때 기록됐다 — 그 사유는 사람이 본 것으로 본다
        # 파싱이 실패한 건에는 ext 가 없다 — 그때는 원본 파일명에서 가져온다(없으면 경로가 점으로 끝난다).
        ext = row.get("ext") or Path(row["src"]).suffix
        v = decide(pred, reg, text=ex_text, scanned=False, ext=ext)
        if v.target is None:
            return {"ok": False, "error": f"고친 라벨로도 경로를 만들 수 없다: {', '.join(v.explain())}"}
        if fix.get("client") and row.get("pred", {}).get("client"):
            ws.learn_alias(row["pred"]["client"], v.client)
        row = {**row, "pred": v.pred, "client": v.client, "category": v.category,
               "doc_type": v.doc_type, "period": v.period, "target": v.target,
               "reasons": [], "explain": [], "fixed_by_human": True}
        ws.append_queue({"id": row["id"], "ts": now(), "state": "ready", "pred": v.pred,
                         "client": v.client, "category": v.category, "doc_type": v.doc_type,
                         "period": v.period, "target": v.target, "reasons": [], "explain": [],
                         "fixed_by_human": True, "note": "사람이 라벨을 고쳤다"})

    if not row.get("target"):
        return {"ok": False, "error": f"보류 상태다. 먼저 고쳐라: {', '.join(row.get('explain') or row.get('reasons', []))}"}

    src = Path(row["src"])
    if not src.exists():
        return {"ok": False, "error": f"원본이 없다: {src}"}
    mv = ws.place(src, row["target"], row["id"])
    ws.append_queue({"id": row["id"], "ts": now(), "state": "approved", "moved_to": mv["dst"],
                     "renamed": mv["renamed"]})
    return {"ok": True, "id": row["id"], "dst": mv["dst"], "renamed": mv["renamed"]}


def reject(ws: Workspace, doc_id: str, why: str = "") -> dict:
    row = ws.get(doc_id)
    if row is None:
        return {"ok": False, "error": f"큐에 없다: {doc_id}"}
    ws.append_queue({"id": row["id"], "ts": now(), "state": "rejected", "note": why})
    return {"ok": True, "id": row["id"]}
