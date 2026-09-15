#!/usr/bin/env python3
"""작업공간과 대장 — 확인 큐·거래처 목록·이동 기록.

작업공간 한 폴더가 사무소 하나다.

    <작업공간>/
      inbox/                들어온 파일
      정리됨/               거래처별 표준 폴더 트리 (규칙이 만든다)
      .nabi/registry.json   대표가 등록한 거래처 목록·별칭
      .nabi/queue.jsonl     확인 큐 대장 — append only
      .nabi/moves.jsonl     이동 기록 — undo 가 이걸 되짚는다

**파일은 사라지지 않는다.** 이동은 move + 기록이고 되돌릴 수 있다. 같은 이름이 이미 있으면 덮어쓰지 않고
`_2` 를 붙이고 그 사실을 기록한다. 대장은 append-only 라 이력이 지워지지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .rules import Registry

QUEUE = "queue.jsonl"
MOVES = "moves.jsonl"
REGISTRY = "registry.json"
STATES = ("held", "ready", "approved", "rejected")


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Workspace:
    root: Path

    def __post_init__(self):
        self.root = Path(self.root).expanduser().resolve()

    # ---------------- 경로
    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def organized(self) -> Path:
        return self.root / "정리됨"

    @property
    def meta(self) -> Path:
        return self.root / ".nabi"

    def init(self) -> None:
        for p in (self.inbox, self.organized, self.meta):
            p.mkdir(parents=True, exist_ok=True)
        if not (self.meta / REGISTRY).exists():
            self.write_registry(Registry(clients=[], aliases={}))

    # ---------------- 거래처 목록
    def read_registry(self) -> Registry:
        p = self.meta / REGISTRY
        if not p.exists():
            return Registry(clients=[], aliases={})
        d = json.loads(p.read_text(encoding="utf-8"))
        return Registry(clients=d.get("clients", []), aliases=d.get("aliases", {}))

    def write_registry(self, reg: Registry) -> None:
        self.meta.mkdir(parents=True, exist_ok=True)
        (self.meta / REGISTRY).write_text(
            json.dumps({"clients": reg.clients, "aliases": reg.aliases}, ensure_ascii=False, indent=1),
            encoding="utf-8")

    def learn_alias(self, wrote: str, means: str) -> None:
        """확인 큐에서 사람이 고친 것은 별칭으로 되돌아온다(기획서 §3 '수정은 기록돼 규칙으로 되돌아간다')."""
        reg = self.read_registry()
        if wrote and means and wrote != means and wrote not in reg.aliases:
            reg.aliases[wrote] = means
            self.write_registry(reg)

    # ---------------- 대장 (append only)
    def _append(self, name: str, row: dict) -> None:
        self.meta.mkdir(parents=True, exist_ok=True)
        with (self.meta / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _read(self, name: str) -> list[dict]:
        p = self.meta / name
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    def append_queue(self, row: dict) -> None:
        self._append(QUEUE, row)

    def append_move(self, row: dict) -> None:
        self._append(MOVES, row)

    def queue(self) -> list[dict]:
        """append-only 대장을 접어 현재 상태를 만든다. 나중 줄이 이긴다."""
        cur: dict[str, dict] = {}
        for row in self._read(QUEUE):
            i = row["id"]
            if i in cur:
                cur[i] = {**cur[i], **row}
            else:
                cur[i] = row
        return list(cur.values())

    def get(self, doc_id: str) -> dict | None:
        cand = [r for r in self.queue() if r["id"] == doc_id or r["id"].startswith(doc_id)]
        return cand[0] if len(cand) == 1 else None

    def seen_hashes(self) -> set[str]:
        return {r["sha256"] for r in self.queue() if r.get("sha256")}

    def moves(self) -> list[dict]:
        return self._read(MOVES)

    # ---------------- 이동
    def place(self, src: Path, rel_target: str, doc_id: str) -> dict:
        """rel_target 은 `거래처/…/파일명` 상대경로. 덮어쓰지 않는다."""
        dst = self.organized / rel_target
        dst.parent.mkdir(parents=True, exist_ok=True)
        renamed = False
        if dst.exists():
            stem, suf, n = dst.stem, dst.suffix, 2
            while dst.exists():
                dst = dst.with_name(f"{stem}_{n}{suf}")
                n += 1
            renamed = True
        shutil.move(str(src), str(dst))
        row = {"id": doc_id, "ts": now(), "src": str(src), "dst": str(dst),
               "rel_target": rel_target, "renamed": renamed, "undone": False}
        self.append_move(row)
        return row

    def undo_last(self) -> dict | None:
        for row in reversed(self.moves()):
            if row.get("undone"):
                continue
            dst, src = Path(row["dst"]), Path(row["src"])
            if not dst.exists():
                self.append_move({**row, "undone": True, "note": "대상 파일이 이미 없다"})
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            back = src
            if back.exists():
                back = src.with_name(f"{src.stem}_되돌림{src.suffix}")
            shutil.move(str(dst), str(back))
            self.append_move({**row, "undone": True, "ts": now(), "restored_to": str(back)})
            self.append_queue({"id": row["id"], "state": "ready", "ts": now(),
                               "note": f"이동을 되돌렸다 → {back}"})
            return {**row, "restored_to": str(back)}
        return None
