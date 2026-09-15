#!/usr/bin/env python3
"""체험 샌드박스 — 방문자 한 명에 작업공간 하나.

심사위원 여럿이 동시에 `/try` 를 만진다. 승인·이동·되돌리기가 서로 섞이면 안 되므로 방문자마다
`samples/inbox` 를 복사한 작업공간을 만들고, 큐는 미리 잰 결과(`recorded.json`)의 행으로 **즉시** 채운다.
파서도 모델도 부르지 않는다 — 행은 같은 파일에서 같은 기기가 이미 낸 것이고 `src` 만 이 샌드박스로 바꾼다.

방문자는 파일을 올릴 수 없다(집 회선의 N100 에 남의 파일을 받지 않는다). 샌드박스는 한동안 안 쓰이면 지운다.
"""
from __future__ import annotations

import json
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from engine.rules import Registry
from engine.store import Workspace


def load_recorded(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def registry_of(recorded: dict) -> Registry:
    """기록의 정답에 나온 거래처가 이 사무소의 등록 목록이다."""
    clients = sorted({d["want"]["client"] for d in recorded["docs"]})
    return Registry(clients=clients, aliases={})


@dataclass
class Sandbox:
    token: str
    ws: Workspace
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_seen = time.time()


class SessionStore:
    """샌드박스 목록. 토큰 → Sandbox. 만료·상한은 여기서 본다."""

    def __init__(self, sessions_dir: Path, samples_dir: Path, recorded: dict,
                 ttl_s: int = 7200, max_sessions: int = 200):
        self.dir = Path(sessions_dir)
        self.samples = Path(samples_dir)
        self.recorded = recorded
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self._lock = threading.Lock()
        self._live: dict[str, Sandbox] = {}
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 만들기·찾기
    def create(self) -> Sandbox:
        with self._lock:
            self._evict_locked()
            token = secrets.token_urlsafe(18)
            root = self.dir / token
            ws = Workspace(root)
            ws.init()
            ws.write_registry(registry_of(self.recorded))
            for d in self.recorded["docs"]:
                src = self.samples / d["file"]
                dst = ws.inbox / d["file"]
                shutil.copy(src, dst)
                ws.append_queue({**d["row"], "src": str(dst)})
            sb = Sandbox(token=token, ws=ws)
            self._live[token] = sb
            return sb

    def get(self, token: str | None) -> Sandbox | None:
        if not token:
            return None
        with self._lock:
            sb = self._live.get(token)
            if sb is None:
                return None
            if time.time() - sb.last_seen > self.ttl_s:
                self._drop_locked(token)
                return None
            sb.touch()
            return sb

    def drop(self, token: str) -> None:
        with self._lock:
            self._drop_locked(token)

    def __len__(self) -> int:
        return len(self._live)

    # ---------------- 정리
    def _drop_locked(self, token: str) -> None:
        sb = self._live.pop(token, None)
        if sb is not None:
            shutil.rmtree(sb.ws.root, ignore_errors=True)

    def _evict_locked(self) -> None:
        now = time.time()
        for t, sb in list(self._live.items()):
            if now - sb.last_seen > self.ttl_s:
                self._drop_locked(t)
        while len(self._live) >= self.max_sessions:
            oldest = min(self._live.values(), key=lambda s: s.last_seen)
            self._drop_locked(oldest.token)

    def sweep(self) -> None:
        with self._lock:
            self._evict_locked()
