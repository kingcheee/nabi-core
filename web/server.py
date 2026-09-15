#!/usr/bin/env python3
"""웹 서버 — 정적 사이트(`site/`) + 확인 큐 API. 파이썬 표준 라이브러리만 쓴다(의존성 0 — 폐PC·N100 에 그대로).

두 모드, 코드는 하나다.

    python3 -m web --demo                 # 체험(/try): 샘플 22건 · 업로드 없음 · 방문자별 샌드박스 · 미리 잰 결과
    python3 -m web --workspace ~/nabi     # 설치된 PC 의 로컬 UI: 작업공간 하나 · inbox 스캔 · 실제 파일

API 는 `/api/...` 와 `/try/api/...` 둘 다 받는다. 정적 호스팅(Vercel)에서 `/try/api/*` 만 이 서버로
rewrite 하면 한 도메인 네 경로가 된다(`site/README.md`). 엔진은 그대로 부른다 — 여기엔 판정 로직이 없다.

    GET  /api/health                 모드·기기·모델 생존·대기열
    GET  /api/state                  큐 전체 + 정리된 트리 (체험이면 세션을 만들고 쿠키를 준다)
    GET  /api/docs/{id}              한 건 상세
    POST /api/docs/{id}/approve      {client?, doc_type?, period?} — 고쳐서 승인도 여기로
    POST /api/docs/{id}/reject       {why?}
    POST /api/docs/{id}/relabel      → 202 {job}. 실제로 모델이 돈다(한 번에 하나, 대기열)
    GET  /api/jobs/{id}              작업 상태 · GET /api/jobs/{id}/events  SSE 로 진행
    POST /api/undo                   마지막 이동 되돌리기
    POST /api/reset                  (체험) 새 샌드박스
    POST /api/scan                   (로컬) inbox 의 새 파일을 큐에 올린다
"""
from __future__ import annotations

import json
import mimetypes
import queue
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import label as model_api
from engine.extract import extract
from engine.labels import CATEGORIES, DOC_TYPES
from engine.pipeline import approve, reject, scan_one
from engine.rules import Registry
from engine.store import Workspace, now, sha256
from web.demo import SessionStore, load_recorded

COOKIE = "nabi_session"
TEXT_HEAD = 800
MAX_BODY = 64 * 1024
HEALTH_TTL_S = 15

Labeler = Callable[[Workspace, Path, Registry], dict]


@dataclass
class Config:
    site_dir: Path
    mode: str = "demo"                       # demo | local
    samples_dir: Path | None = None          # demo: 샘플 원본(inbox 폴더)
    recorded_path: Path | None = None        # demo: 미리 잰 결과
    sessions_dir: Path | None = None         # demo: 샌드박스 저장소
    workspace: Path | None = None            # local: 작업공간
    model: str = "minicpm5"
    model_url: str = model_api.DEFAULT_URL
    device: str = ""
    labeler: Labeler | None = None           # (ws, path, reg) → 큐 행. 기본은 scan_one 실호출. 테스트가 주입한다
    health: Callable[[], bool] | None = None  # 모델 서버 생존 확인. 기본은 llama-server /health
    session_ttl_s: int = 7200
    max_sessions: int = 200

    def __post_init__(self):
        self.site_dir = Path(self.site_dir).resolve()
        if self.mode not in ("demo", "local"):
            raise ValueError(f"mode 는 demo | local: {self.mode}")
        if self.mode == "demo" and not (self.samples_dir and self.recorded_path and self.sessions_dir):
            raise ValueError("demo 모드는 samples_dir · recorded_path · sessions_dir 가 필요하다")
        if self.mode == "local" and not self.workspace:
            raise ValueError("local 모드는 workspace 가 필요하다")


def default_labeler(cfg: Config) -> Labeler:
    def run(ws: Workspace, path: Path, reg: Registry) -> dict:
        if not model_api.health(cfg.model_url, timeout=3):
            raise RuntimeError(f"모델 서버가 응답하지 않는다: {cfg.model_url}")
        return scan_one(ws, path, reg, cfg.model, cfg.model_url)
    return run


def text_head(path: Path) -> str:
    try:
        return extract(path).text[:TEXT_HEAD]
    except Exception as e:
        return f"(본문을 읽지 못했다: {type(e).__name__})"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- 실시간 작업 (한 번에 하나)

@dataclass
class Job:
    id: str
    token: str | None
    doc_id: str
    name: str
    state: str = "queued"                    # queued | running | done | failed
    submitted: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    error: str = ""
    row: dict | None = None

    def snapshot(self, position: int) -> dict:
        t0 = self.started or self.submitted
        end = self.finished or time.time()
        return {"job": self.id, "doc": self.doc_id, "name": self.name, "state": self.state,
                "position": position, "elapsed_s": round(end - t0, 1), "error": self.error, "row": self.row}


class Jobs:
    """모델은 CPU 하나를 통째로 쓰므로 한 번에 한 건만 돈다. 나머지는 줄을 선다."""

    def __init__(self, run: Callable[[Job], dict]):
        self._run = run
        self._q: queue.Queue[Job] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True, name="nabi-jobs").start()

    def submit(self, token: str | None, doc_id: str, name: str) -> Job:
        job = Job(id=secrets.token_hex(6), token=token, doc_id=doc_id, name=name)
        with self._lock:
            self._jobs[job.id] = job
        self._q.put(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def position(self, job: Job) -> int:
        """앞에 몇 건이 기다리나. 도는 중이면 0."""
        if job.state != "queued":
            return 0
        with self._lock:
            ahead = [j for j in self._jobs.values() if j.state == "queued" and j.submitted < job.submitted]
        return len(ahead) + (1 if any(j.state == "running" for j in self._jobs.values()) else 0)

    def queued(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.state in ("queued", "running"))

    def _loop(self) -> None:
        while True:
            job = self._q.get()
            job.state, job.started = "running", time.time()
            try:
                job.row = self._run(job)
                job.state = "done"
            except Exception as e:                       # 모델·파서·세션 만료 — 이유를 그대로 화면에
                job.error = str(e) or type(e).__name__
                job.state = "failed"
            job.finished = time.time()
            with self._lock:                             # 끝난 작업은 한동안만 기억한다
                old = [i for i, j in self._jobs.items() if j.finished and time.time() - j.finished > 600]
                for i in old:
                    self._jobs.pop(i, None)


# ---------------------------------------------------------------- 앱

class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.labeler = cfg.labeler or default_labeler(cfg)
        self.health_fn = cfg.health or (lambda: model_api.health(cfg.model_url, timeout=2))
        self._health: tuple[float, bool] = (0.0, False)
        self.recorded: dict = {"meta": {}, "docs": []}
        self.store: SessionStore | None = None
        self.ws: Workspace | None = None
        if cfg.mode == "demo":
            self.recorded = load_recorded(cfg.recorded_path)
            self.store = SessionStore(cfg.sessions_dir, cfg.samples_dir, self.recorded,
                                      ttl_s=cfg.session_ttl_s, max_sessions=cfg.max_sessions)
        else:
            self.ws = Workspace(cfg.workspace)
            self.ws.init()
        self._samples = {d["file"]: d for d in self.recorded["docs"]}
        self.jobs = Jobs(self._run_job)

    # ---------------- 공통
    def model_alive(self) -> bool:
        at, ok = self._health
        if time.time() - at > HEALTH_TTL_S:
            try:
                ok = bool(self.health_fn())
            except Exception:
                ok = False
            self._health = (time.time(), ok)
        return ok

    def workspace(self, token: str | None) -> tuple[Workspace, str | None]:
        """이 요청의 작업공간. 체험이면 (세션이 없을 때) 새로 만들고 새 토큰을 돌려준다."""
        if self.cfg.mode == "local":
            return self.ws, None
        sb = self.store.get(token)
        if sb is not None:
            return sb.ws, None
        sb = self.store.create()
        return sb.ws, sb.token

    def _doc(self, ws: Workspace, doc_id: str) -> dict:
        row = ws.get(doc_id)
        if row is None:
            raise ApiError(404, f"큐에 없다: {doc_id}")
        return row

    def _view(self, row: dict) -> dict:
        s = self._samples.get(row.get("name", ""))
        sample = None
        if s is not None:
            sample = {"id": s["sample_id"], "kind": s["kind"], "note": s.get("note", ""), "want": s["want"]}
        return {**row, "sample": sample, "text_head": row.get("text_head") or (s or {}).get("text_head", ""),
                "live": bool(row.get("live"))}

    @staticmethod
    def _tree(ws: Workspace) -> list[str]:
        if not ws.organized.exists():
            return []
        return sorted(p.relative_to(ws.organized).as_posix() for p in ws.organized.rglob("*") if p.is_file())

    # ---------------- 조회
    def health(self) -> dict:
        return {"mode": self.cfg.mode, "device": self.cfg.device or self.recorded["meta"].get("device", ""),
                "model": self.cfg.model, "model_alive": self.model_alive(), "queue_len": self.jobs.queued(),
                "sessions": len(self.store) if self.store else 0, "recorded": self.recorded["meta"]}

    def state(self, ws: Workspace, token: str | None) -> dict:
        return {"mode": self.cfg.mode, "session": token, "recorded": self.recorded["meta"],
                "device": self.cfg.device or self.recorded["meta"].get("device", ""),
                "model_alive": self.model_alive(), "queue_len": self.jobs.queued(),
                "docs": [self._view(r) for r in ws.queue()], "tree": self._tree(ws),
                "moves": sum(1 for m in ws.moves() if not m.get("undone")),
                "registry": ws.read_registry().clients,
                "vocab": {"doc_types": DOC_TYPES, "categories": CATEGORIES}}

    def detail(self, ws: Workspace, doc_id: str) -> dict:
        return self._view(self._doc(ws, doc_id))

    # ---------------- 동작
    def approve(self, ws: Workspace, doc_id: str, body: dict) -> dict:
        row = self._doc(ws, doc_id)
        fix = {k: (body.get(k) or "").strip() for k in ("client", "doc_type", "period")}
        fix = {k: v for k, v in fix.items() if v}
        r = approve(ws, row["id"], fix=fix or None)
        if not r["ok"]:
            raise ApiError(409, r["error"])
        return r

    def reject(self, ws: Workspace, doc_id: str, body: dict) -> dict:
        row = self._doc(ws, doc_id)
        return reject(ws, row["id"], why=(body.get("why") or "").strip())

    def undo(self, ws: Workspace) -> dict:
        r = ws.undo_last()
        if r is None:
            raise ApiError(409, "되돌릴 이동이 없다")
        return {"ok": True, **r}

    def relabel(self, ws: Workspace, token: str | None, doc_id: str) -> Job:
        row = self._doc(ws, doc_id)
        if row.get("state") == "approved":
            raise ApiError(409, "이미 승인·이동된 서류다. 되돌린 뒤에 다시 잴 수 있다")
        if not Path(row["src"]).exists():
            raise ApiError(409, f"원본이 없다: {row.get('name')}")
        return self.jobs.submit(token, row["id"], row.get("name", ""))

    def _run_job(self, job: Job) -> dict:
        if self.cfg.mode == "demo":
            sb = self.store.get(job.token)
            if sb is None:
                raise RuntimeError("세션이 만료됐다. 화면을 새로 고쳐라")
            ws = sb.ws
        else:
            ws = self.ws
        row = ws.get(job.doc_id)
        if row is None:
            raise RuntimeError("큐에서 사라진 서류다")
        fresh = self.labeler(ws, Path(row["src"]), ws.read_registry())
        if "model_failed" in (fresh.get("reasons") or []):
            raise RuntimeError(fresh.get("error") or "모델이 실패했다")
        fresh = {**fresh, "live": True, "live_at": now()}
        if "text_head" not in row:
            fresh["text_head"] = text_head(Path(row["src"]))
        ws.append_queue(fresh)
        return self._view(ws.get(job.doc_id))

    def reset(self, token: str | None) -> tuple[dict, str]:
        if self.cfg.mode != "demo":
            raise ApiError(404, "체험 모드에만 있다")
        if token:
            self.store.drop(token)
        sb = self.store.create()
        return {"ok": True, "session": sb.token}, sb.token

    def scan(self, ws: Workspace) -> dict:
        if self.cfg.mode != "local":
            raise ApiError(404, "로컬 모드에만 있다 — 체험은 파일을 받지 않는다")
        reg = ws.read_registry()
        seen = ws.seen_hashes()
        n, errors = 0, []
        for p in sorted(q for q in ws.inbox.iterdir() if q.is_file() and not q.name.startswith(".")):
            if sha256(p) in seen:
                continue
            try:
                row = self.labeler(ws, p, reg)
            except Exception as e:
                errors.append(f"{p.name}: {e}")
                break                                    # 모델이 없으면 나머지도 안 된다
            ws.append_queue({**row, "text_head": text_head(p)})
            n += 1
        if errors and n == 0:
            raise ApiError(503, errors[0])
        return {"ok": True, "scanned": n, "errors": errors}


# ---------------------------------------------------------------- HTTP

_ROUTES = [
    ("GET", re.compile(r"^/api/health$"), "health"),
    ("GET", re.compile(r"^/api/state$"), "state"),
    ("GET", re.compile(r"^/api/docs/(?P<id>[^/]+)$"), "detail"),
    ("POST", re.compile(r"^/api/docs/(?P<id>[^/]+)/approve$"), "approve"),
    ("POST", re.compile(r"^/api/docs/(?P<id>[^/]+)/reject$"), "reject"),
    ("POST", re.compile(r"^/api/docs/(?P<id>[^/]+)/relabel$"), "relabel"),
    ("GET", re.compile(r"^/api/jobs/(?P<id>[^/]+)$"), "job"),
    ("GET", re.compile(r"^/api/jobs/(?P<id>[^/]+)/events$"), "events"),
    ("POST", re.compile(r"^/api/undo$"), "undo"),
    ("POST", re.compile(r"^/api/reset$"), "reset"),
    ("POST", re.compile(r"^/api/scan$"), "scan"),
]

_NO_CACHE = {".html", ".json", ".css", ".js"}


class Handler(BaseHTTPRequestHandler):
    server: "Server"
    protocol_version = "HTTP/1.0"

    # ---------------- 진입
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        app = self.server.app
        path = urlsplit(self.path).path
        api = path[4:] if path.startswith("/try/api/") else path
        if api.startswith("/api/"):
            self._api(method, api)
            return
        if method != "GET":
            self._send_json(405, {"ok": False, "error": "GET 만"})
            return
        self._static(unquote(path))

    # ---------------- API
    def _api(self, method: str, path: str) -> None:
        app = self.server.app
        for m, rx, name in _ROUTES:
            mt = rx.match(path)
            if not mt:
                continue
            if m != method:
                self._send_json(405, {"ok": False, "error": f"{m} 만"})
                return
            try:
                getattr(self, "_r_" + name)(**mt.groupdict())
            except ApiError as e:
                self._send_json(e.status, {"ok": False, "error": e.message})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:                       # 예상 못 한 것 — 500 으로 이유를 남긴다
                self.log_error("500 %s %s: %r", method, path, e)
                self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self._send_json(404, {"ok": False, "error": "없는 API"})

    def _token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = SimpleCookie()
        try:
            c.load(raw)
        except Exception:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def _ws(self) -> tuple[Workspace, str | None, str | None]:
        """(작업공간, 지금 세션 토큰, 새로 발급한 토큰)"""
        app = self.server.app
        token = self._token()
        ws, fresh = app.workspace(token)
        return ws, (fresh or token), fresh

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "본문이 너무 크다")
        raw = self.rfile.read(n) if n else b""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            raise ApiError(400, "JSON 이 아니다")
        if not isinstance(body, dict):
            raise ApiError(400, "JSON 객체여야 한다")
        return body

    def _r_health(self):
        self._send_json(200, self.server.app.health())

    def _r_state(self):
        ws, token, fresh = self._ws()
        self._send_json(200, self.server.app.state(ws, token), cookie=fresh)

    def _r_detail(self, id):
        ws, _, fresh = self._ws()
        self._send_json(200, self.server.app.detail(ws, id), cookie=fresh)

    def _r_approve(self, id):
        body = self._body()
        ws, _, fresh = self._ws()
        self._send_json(200, self.server.app.approve(ws, id, body), cookie=fresh)

    def _r_reject(self, id):
        body = self._body()
        ws, _, fresh = self._ws()
        self._send_json(200, self.server.app.reject(ws, id, body), cookie=fresh)

    def _r_relabel(self, id):
        self._body()
        ws, token, fresh = self._ws()
        job = self.server.app.relabel(ws, token, id)
        self._send_json(202, {"ok": True, "job": job.id, **job.snapshot(self.server.app.jobs.position(job))},
                        cookie=fresh)

    def _r_undo(self):
        self._body()
        ws, _, fresh = self._ws()
        self._send_json(200, self.server.app.undo(ws), cookie=fresh)

    def _r_reset(self):
        self._body()
        out, token = self.server.app.reset(self._token())
        self._send_json(200, out, cookie=token)

    def _r_scan(self):
        self._body()
        ws, _, fresh = self._ws()
        self._send_json(200, self.server.app.scan(ws), cookie=fresh)

    def _job(self, job_id: str) -> Job:
        job = self.server.app.jobs.get(job_id)
        if job is None:
            raise ApiError(404, "없는 작업(또는 10분이 지나 잊었다)")
        return job

    def _r_job(self, id):
        job = self._job(id)
        self._send_json(200, job.snapshot(self.server.app.jobs.position(job)))

    def _r_events(self, id):
        job = self._job(id)
        jobs = self.server.app.jobs
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last, beat = None, time.time()
        while True:
            snap = job.snapshot(jobs.position(job))
            s = json.dumps(snap, ensure_ascii=False)
            if s != last:
                self.wfile.write(f"event: state\ndata: {s}\n\n".encode())
                self.wfile.flush()
                last = s
            if snap["state"] in ("done", "failed"):
                break
            if time.time() - beat > 10:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                beat = time.time()
            time.sleep(0.5)

    # ---------------- 정적
    def _static(self, path: str) -> None:
        site = self.server.app.cfg.site_dir
        parts = [p for p in path.split("/") if p]
        if any(p in ("..", ".") or p.startswith("..") for p in parts):
            self._send_json(404, {"ok": False, "error": "없는 경로"})
            return
        target = site.joinpath(*parts) if parts else site
        try:
            resolved = target.resolve()
            resolved.relative_to(site)
        except (ValueError, OSError):
            self._send_json(404, {"ok": False, "error": "없는 경로"})
            return
        if resolved.is_dir():
            resolved = resolved / "index.html"
        if not resolved.is_file():
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("없는 페이지\n".encode())
            return
        ctype, _ = mimetypes.guess_type(str(resolved))
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
            ctype += "; charset=utf-8"
        data = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if resolved.suffix in _NO_CACHE else "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    # ---------------- 응답
    def _send_json(self, status: int, body: dict, cookie: str | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):                   # 정적·이벤트 스트림은 조용히
        if self.server.quiet or "/events" in self.path or not self.path.split("?")[0].endswith(
                ("/state", "/approve", "/reject", "/relabel", "/undo", "/reset", "/scan", "/health")):
            return
        sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app: App, quiet: bool = True):
        super().__init__(addr, Handler)
        self.app = app
        self.quiet = quiet


def make_server(cfg: Config, host: str = "127.0.0.1", port: int = 8098, quiet: bool = True) -> Server:
    mimetypes.add_type("application/javascript", ".js")
    return Server((host, port), App(cfg), quiet=quiet)
