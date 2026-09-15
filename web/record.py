#!/usr/bin/env python3
"""미리 잰 결과 — 샘플 전건을 엔진(파서→모델→규칙)에 돌려 `site/try/recorded.json` 을 만든다.

    python3 -m web.record --device "Intel N100 8GB (1호기)" [--threads 3] [--model minicpm5] [--url ...]

체험(`/try`)은 방문자가 왔을 때 모델을 부르지 않는다 — N100 은 서류 1건에 약 80초라 기다리게 할 수 없다.
대신 **같은 기기에서 미리 잰** 이 파일로 큐를 즉시 채우고, 「지금 다시 재기」를 눌렀을 때만 실제로 돈다.
그래서 기록에는 어느 기기·언제·어떤 모델인지가 반드시 붙는다. 숫자를 지어 넣지 않는다 — 이 파일은
이 스크립트로만 만든다.

행(`row`)은 엔진 `scan_one` 이 낸 큐 레코드 그대로다. 그 옆에 체험 화면이 쓸 것만 덧붙인다 —
샘플 정답(`want`, 화면에서 「정답과 대조」로만 쓴다), 모델이 읽은 본문 머리(`text_head`), 형식 표시(`kind`).
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.extract import extract
from engine.label import DEFAULT_URL, health
from engine.pipeline import scan_one
from engine.rules import Registry
from engine.store import Workspace

TEXT_HEAD = 800
OUT = ROOT / "site" / "try" / "recorded.json"

# labels.json 의 format · ocr_kind → 화면에 보이는 형식 이름
KIND = {"scan": "스캔", "jpg": "사진", "fax": "팩스"}
EXT_KIND = {"pdf": "PDF", "xlsx": "엑셀", "xls": "엑셀", "docx": "워드", "hwpx": "한글", "hwp": "한글",
            "csv": "CSV", "txt": "텍스트", "jpg": "사진", "png": "사진"}


def kind_of(sample: dict) -> str:
    if sample.get("ocr_kind"):
        if "팩스" in (sample.get("note") or ""):
            return "팩스"
        return KIND.get(sample["ocr_kind"], "스캔")
    return EXT_KIND.get(sample.get("format", ""), sample.get("format", "").upper())


def registry_from(labels: list[dict]) -> Registry:
    """정답 라벨의 거래처가 곧 「대표가 등록한 목록」이다(bench/label_all.py 와 같은 기준)."""
    clients = sorted({r["client"] for r in labels})
    aliases = {r["client_short"]: r["client"] for r in labels if r.get("client_short")}
    return Registry(clients=clients, aliases=aliases)


def build_recorded(samples_dir: Path, labels: list[dict], registry: Registry,
                   labeler: Callable[[Workspace, Path, Registry], dict], meta: dict,
                   log: Callable[[str], None] = lambda s: None) -> dict:
    """샘플마다 엔진 행 + 본문 머리 + 정답. labeler 는 (ws, path, reg) → 큐 행. 파일은 움직이지 않는다."""
    with tempfile.TemporaryDirectory(prefix="nabi-record-") as tmp:
        ws = Workspace(Path(tmp) / "ws")
        ws.init()
        ws.write_registry(registry)
        docs = []
        for s in labels:
            p = samples_dir / s["file"]
            row = labeler(ws, p, registry)
            row["src"] = s["file"]                       # 임시 경로는 남기지 않는다 — 샌드박스가 자기 경로로 바꾼다
            try:
                head = extract(p).text[:TEXT_HEAD]
            except Exception as e:                       # 파싱 실패는 행에 이미 적혀 있다
                head = f"(본문을 읽지 못했다: {type(e).__name__})"
            want = {"client": s["client"], "category": s["category"], "doc_type": s["doc_type"],
                    "period": s["period"]["normalized"]}
            docs.append({"sample_id": s["id"], "file": s["file"], "kind": kind_of(s),
                         "note": s.get("note", ""), "want": want, "text_head": head, "row": row})
            log(f"{s['id']} {row.get('state','?'):8s} {row.get('label_s', 0) or 0:6.1f}s  {s['file']}")
    return {"meta": {**meta, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "count": len(docs)},
            "docs": docs}


def guess_device() -> str:
    cpu = ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith(("model name", "hardware")):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return f"{platform.node()} ({cpu})" if cpu else platform.node()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default=guess_device(), help="기록에 남길 기기 이름")
    ap.add_argument("--threads", type=int, default=0, help="llama-server 에 준 스레드 수(기록용)")
    ap.add_argument("--model", default="minicpm5")
    ap.add_argument("--model-file", default="MiniCPM5-2B-Q4_K_M.gguf")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()

    if not health(a.url):
        print(f"모델 서버가 없다: {a.url}  (python3 -m engine serve 참고)")
        return 2
    labels = json.loads((ROOT / "samples" / "labels.json").read_text(encoding="utf-8"))
    reg = registry_from(labels)

    def labeler(ws, path, reg):
        return scan_one(ws, path, reg, a.model, a.url)

    meta = {"device": a.device, "model": a.model, "model_file": a.model_file, "threads": a.threads,
            "url": a.url}
    rec = build_recorded(ROOT / "samples" / "inbox", labels, reg, labeler, meta, log=print)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    n_ready = sum(1 for d in rec["docs"] if d["row"].get("state") == "ready")
    print(f"\n{a.out}  — {len(rec['docs'])}건 · 확정 {n_ready} · 보류 {len(rec['docs']) - n_ready} · {a.device}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
