#!/usr/bin/env python3
"""웹 서버 테스트 — 진짜 HTTP 로 두드린다. 모델은 부르지 않는다(labeler 를 주입해 모델 출력을 넣는다).

체험(`--demo`)은 방문자마다 샌드박스를 받고, 미리 잰 결과(recorded.json)로 큐가 즉시 채워지며,
승인·되돌리기·다시 재기가 그 안에서만 일어난다. 로컬 모드는 작업공간 하나를 그대로 쓴다.
"""
import http.client
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.pipeline import scan_one
from engine.rules import Registry
from engine.store import Workspace
from web.record import build_recorded
from web.server import Config, make_server

SRC = ROOT / "samples" / "inbox"
SITE = ROOT / "site"

# 샘플 세 건 — 하나는 확정(ready), 하나는 보류(held: 미등록 거래처), 하나는 확정
PRED = {
    "급여 3월 최종(2).xlsx": {"client": "대성정밀공업사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-04"},
    "Book1.xlsx": {"client": "가상상사", "category": "신고증빙", "doc_type": "카드매출매입내역", "period": "2026-Q2"},
    "사업자등록증.pdf": {"client": "한빛나루식당", "category": "기본서류", "doc_type": "사업자등록증", "period": "PERMANENT"},
}
LABELS = [
    {"id": "d07", "file": "급여 3월 최종(2).xlsx", "client": "대성정밀공업사", "client_short": "대성정밀",
     "category": "정기증빙", "doc_type": "급여대장", "period": {"cycle": "M", "normalized": "2026-04"},
     "format": "xlsx", "scanned": False, "ocr_kind": None, "note": ""},
    {"id": "d16", "file": "Book1.xlsx", "client": "푸른솔외국어학원", "client_short": "푸른솔",
     "category": "신고증빙", "doc_type": "카드매출매입내역", "period": {"cycle": "Q", "normalized": "2026-Q2"},
     "format": "xlsx", "scanned": False, "ocr_kind": None, "note": ""},
    {"id": "d01", "file": "사업자등록증.pdf", "client": "한빛나루식당", "client_short": "한빛나루",
     "category": "기본서류", "doc_type": "사업자등록증", "period": {"cycle": "PERMANENT", "normalized": "PERMANENT"},
     "format": "pdf", "scanned": True, "ocr_kind": "scan", "note": "스캔본"},
]
REG = Registry(clients=["대성정밀공업사", "한빛나루식당", "푸른솔외국어학원"], aliases={"대성정밀": "대성정밀공업사"})


def fake_labeler(ws, path, reg):
    """모델 대신 — 파서·규칙은 진짜로 돌고 모델 출력만 주입된다."""
    return scan_one(ws, path, reg, "fake", "", offline_pred=PRED[path.name])


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    for name in PRED:
        shutil.copy(SRC / name, d / name)
    return d


@pytest.fixture(scope="module")
def recorded_path(tmp_path_factory, samples):
    """미리 잰 결과 — 진짜 엔진(파서+규칙)으로 만들되 모델 출력은 주입."""
    rec = build_recorded(samples_dir=samples, labels=LABELS, registry=REG, labeler=fake_labeler,
                         meta={"device": "테스트 기기", "model": "fake", "model_file": "없음", "threads": 1})
    p = tmp_path_factory.mktemp("rec") / "recorded.json"
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return p


def start(cfg):
    srv = make_server(cfg, host="127.0.0.1", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


@pytest.fixture
def demo(tmp_path, samples, recorded_path):
    cfg = Config(site_dir=SITE, mode="demo", samples_dir=samples, recorded_path=recorded_path,
                 sessions_dir=tmp_path / "sessions", labeler=fake_labeler, health=lambda: True)
    srv = start(cfg)
    yield srv
    srv.shutdown()


class Visitor:
    """쿠키 한 벌을 가진 방문자."""

    def __init__(self, srv):
        self.port = srv.server_address[1]
        self.cookie = ""

    def req(self, method, path, body=None, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Cookie": self.cookie} if self.cookie else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        payload = r.read()
        if raw:
            return r.status, r.getheader("Content-Type", ""), payload
        return r.status, (json.loads(payload) if payload else None)

    def state(self):
        st, body = self.req("GET", "/try/api/state")
        assert st == 200, body
        return body

    def doc(self, state, name):
        return next(d for d in state["docs"] if d["name"] == name)


# ---------------------------------------------------------------- 체험(demo)

def test_상태를_요청하면_세션이_생기고_미리_잰_결과로_큐가_차_있다(demo):
    v = Visitor(demo)
    st = v.state()
    assert v.cookie.startswith("nabi_session=")
    assert st["mode"] == "demo"
    assert st["recorded"]["device"] == "테스트 기기"
    assert len(st["docs"]) == len(LABELS)
    by = {d["name"]: d for d in st["docs"]}
    assert by["급여 3월 최종(2).xlsx"]["state"] == "ready"
    assert by["Book1.xlsx"]["state"] == "held" and "client_unregistered" in by["Book1.xlsx"]["reasons"]
    assert by["사업자등록증.pdf"]["text_head"].strip()          # 모델이 읽은 본문 머리
    assert by["Book1.xlsx"]["sample"]["want"]["client"] == "푸른솔외국어학원"
    assert all(not d["live"] for d in st["docs"])
    assert len(st["vocab"]["doc_types"]) == 15 and st["vocab"]["categories"] == ["기본서류", "정기증빙", "신고증빙"]
    assert st["registry"] == ["대성정밀공업사", "푸른솔외국어학원", "한빛나루식당"]


def test_승인하면_샌드박스_안에서만_옮겨지고_트리에_나타난다(demo, samples):
    v = Visitor(demo)
    d = v.doc(v.state(), "급여 3월 최종(2).xlsx")
    st, r = v.req("POST", f"/try/api/docs/{d['id']}/approve", {})
    assert st == 200 and r["ok"], r
    after = v.state()
    assert v.doc(after, "급여 3월 최종(2).xlsx")["state"] == "approved"
    assert "대성정밀공업사/2026/01_원천세_급여/대성정밀공업사_2026-04_급여대장.xlsx" in after["tree"]
    assert (samples / "급여 3월 최종(2).xlsx").exists(), "원본 샘플이 움직였다"
    assert Path(r["dst"]).exists() and str(Path(r["dst"])).startswith(str(demo.app.cfg.sessions_dir))


def test_방문자가_다르면_승인이_섞이지_않는다(demo):
    a, b = Visitor(demo), Visitor(demo)
    da = a.doc(a.state(), "급여 3월 최종(2).xlsx")
    b.state()
    assert a.req("POST", f"/try/api/docs/{da['id']}/approve", {})[1]["ok"]
    assert b.doc(b.state(), "급여 3월 최종(2).xlsx")["state"] == "ready"
    assert b.state()["tree"] == []


def test_보류된_서류를_고쳐서_승인하면_별칭이_학습되고_경로가_생긴다(demo):
    v = Visitor(demo)
    d = v.doc(v.state(), "Book1.xlsx")
    st, r = v.req("POST", f"/try/api/docs/{d['id']}/approve", {"client": "푸른솔외국어학원"})
    assert st == 200 and r["ok"], r
    after = v.doc(v.state(), "Book1.xlsx")
    assert after["state"] == "approved" and after["fixed_by_human"]
    assert "푸른솔외국어학원/2026/03_부가세/푸른솔외국어학원_2026-Q2_카드매출매입내역.xlsx" in v.state()["tree"]


def test_보류된_서류를_안_고치고_승인하면_거절된다(demo):
    v = Visitor(demo)
    d = v.doc(v.state(), "Book1.xlsx")
    st, r = v.req("POST", f"/try/api/docs/{d['id']}/approve", {})
    assert st == 409 and not r["ok"]
    assert v.doc(v.state(), "Book1.xlsx")["state"] == "held"


def test_되돌리기는_마지막_이동을_원위치한다(demo):
    v = Visitor(demo)
    d = v.doc(v.state(), "급여 3월 최종(2).xlsx")
    v.req("POST", f"/try/api/docs/{d['id']}/approve", {})
    st, r = v.req("POST", "/try/api/undo", {})
    assert st == 200 and r["ok"], r
    after = v.state()
    assert v.doc(after, "급여 3월 최종(2).xlsx")["state"] == "ready"
    assert after["tree"] == []
    assert Path(r["restored_to"]).exists()


def test_거절하면_상태가_거절이_되고_파일은_그대로다(demo):
    v = Visitor(demo)
    d = v.doc(v.state(), "사업자등록증.pdf")
    st, r = v.req("POST", f"/try/api/docs/{d['id']}/reject", {"why": "샘플 아님"})
    assert st == 200 and r["ok"]
    assert v.doc(v.state(), "사업자등록증.pdf")["state"] == "rejected"
    assert Path(d["src"]).exists()


def read_events(v, path):
    c = http.client.HTTPConnection("127.0.0.1", v.port, timeout=20)
    c.request("GET", path, headers={"Cookie": v.cookie})
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type", "").startswith("text/event-stream")
    events = []
    for raw in r:
        line = raw.decode().rstrip("\n")
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def test_다시_재기는_모델을_거쳐_큐를_갱신하고_진행을_스트리밍한다(demo):
    v = Visitor(demo)
    d = v.doc(v.state(), "사업자등록증.pdf")
    st, r = v.req("POST", f"/try/api/docs/{d['id']}/relabel", {})
    assert st == 202 and r["job"], r
    events = read_events(v, f"/try/api/jobs/{r['job']}/events")
    states = [e["state"] for e in events]
    assert states[-1] == "done", events
    assert "running" in states or "queued" in states
    after = v.doc(v.state(), "사업자등록증.pdf")
    assert after["live"] and after["state"] == "ready" and after["model"] == "fake"
    assert events[-1]["row"]["id"] == d["id"]


def test_승인된_서류는_다시_잴_수_없다(demo):
    v = Visitor(demo)
    d = v.doc(v.state(), "급여 3월 최종(2).xlsx")
    v.req("POST", f"/try/api/docs/{d['id']}/approve", {})
    st, r = v.req("POST", f"/try/api/docs/{d['id']}/relabel", {})
    assert st == 409


def test_모델이_실패하면_작업은_실패로_끝나고_큐는_그대로다(tmp_path, samples, recorded_path):
    def broken(ws, path, reg):
        raise RuntimeError("모델 서버가 응답하지 않는다")
    cfg = Config(site_dir=SITE, mode="demo", samples_dir=samples, recorded_path=recorded_path,
                 sessions_dir=tmp_path / "s", labeler=broken, health=lambda: False)
    srv = start(cfg)
    try:
        v = Visitor(srv)
        d = v.doc(v.state(), "사업자등록증.pdf")
        st, r = v.req("POST", f"/try/api/docs/{d['id']}/relabel", {})
        assert st == 202
        events = read_events(v, f"/try/api/jobs/{r['job']}/events")
        assert events[-1]["state"] == "failed" and "응답하지" in events[-1]["error"]
        after = v.doc(v.state(), "사업자등록증.pdf")
        assert not after["live"] and after["state"] == "ready"
        assert v.state()["model_alive"] is False
    finally:
        srv.shutdown()


def test_초기화하면_새_샌드박스를_받는다(demo):
    v = Visitor(demo)
    d = v.doc(v.state(), "급여 3월 최종(2).xlsx")
    v.req("POST", f"/try/api/docs/{d['id']}/approve", {})
    old = v.cookie
    st, r = v.req("POST", "/try/api/reset", {})
    assert st == 200 and r["ok"]
    assert v.cookie != old
    fresh = v.state()
    assert v.doc(fresh, "급여 3월 최종(2).xlsx")["state"] == "ready" and fresh["tree"] == []


def test_체험에서는_업로드도_스캔도_없다(demo):
    v = Visitor(demo)
    v.state()
    assert v.req("POST", "/try/api/scan", {})[0] == 404


def test_상세는_본문_머리와_판정_설명을_준다(demo):
    v = Visitor(demo)
    d = v.doc(v.state(), "Book1.xlsx")
    st, r = v.req("GET", f"/try/api/docs/{d['id']}")
    assert st == 200 and r["id"] == d["id"]
    assert r["text_head"] and r["explain"]
    assert v.req("GET", "/try/api/docs/nope")[0] == 404


def test_건강_상태는_모드와_기기와_대기열을_말한다(demo):
    v = Visitor(demo)
    st, r = v.req("GET", "/try/api/health")
    assert st == 200
    assert r["mode"] == "demo" and r["device"] == "테스트 기기" and r["queue_len"] == 0
    assert r["model_alive"] is True


# ---------------------------------------------------------------- 정적 파일

def test_정적_파일을_서빙하고_상위_경로_탈출을_막는다(demo):
    v = Visitor(demo)
    st, ct, body = v.req("GET", "/", raw=True)
    assert st == 200 and ct.startswith("text/html") and b"<h1>" in body
    st, ct, _ = v.req("GET", "/try", raw=True)
    assert st == 200 and ct.startswith("text/html")
    st, ct, _ = v.req("GET", "/try/", raw=True)
    assert st == 200 and ct.startswith("text/html")
    for bad in ("/../engine/labels.py", "/try/../../engine/labels.py", "/%2e%2e/engine/labels.py"):
        st, _, _ = v.req("GET", bad, raw=True)
        assert st in (403, 404), bad
    assert v.req("GET", "/%EC%97%86%EB%8A%94-%ED%8E%98%EC%9D%B4%EC%A7%80", raw=True)[0] == 404


# ---------------------------------------------------------------- 교차 출처 (정적 호스팅 + 인스턴스가 다른 origin)

def raw(srv, method, path, headers=None, body=None):
    """쿠키를 안 들고 가는 날것 요청 — (status, headers dict, body bytes)."""
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()


@pytest.fixture
def cors_demo(tmp_path, samples, recorded_path):
    cfg = Config(site_dir=SITE, mode="demo", samples_dir=samples, recorded_path=recorded_path,
                 sessions_dir=tmp_path / "sessions", labeler=fake_labeler, health=lambda: True,
                 cors_origins=["https://kingcheee.github.io"])
    srv = start(cfg)
    yield srv
    srv.shutdown()


def test_허용된_출처에만_CORS_헤더를_주고_프리플라이트에_답한다(cors_demo, demo):
    ok = "https://kingcheee.github.io"
    # 프리플라이트
    st, h, _ = raw(cors_demo, "OPTIONS", "/try/api/docs/x/approve",
                   {"Origin": ok, "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type, x-nabi-session"})
    assert st == 204
    assert h["access-control-allow-origin"] == ok
    assert "POST" in h["access-control-allow-methods"]
    assert "x-nabi-session" in h["access-control-allow-headers"].lower()
    assert "content-type" in h["access-control-allow-headers"].lower()
    # 실제 요청 — JSON 과 SSE 모두
    st, h, _ = raw(cors_demo, "GET", "/try/api/state", {"Origin": ok})
    assert st == 200 and h["access-control-allow-origin"] == ok and "origin" in h["vary"].lower()
    st, h, _ = raw(cors_demo, "GET", "/try/api/health", {"Origin": ok})
    assert h["access-control-allow-origin"] == ok
    # 다른 출처 · 설정 없는 서버 → 헤더 없음
    st, h, _ = raw(cors_demo, "GET", "/try/api/state", {"Origin": "https://evil.example"})
    assert st == 200 and "access-control-allow-origin" not in h
    st, h, _ = raw(cors_demo, "OPTIONS", "/try/api/state", {"Origin": "https://evil.example",
                                                             "Access-Control-Request-Method": "GET"})
    assert st == 403
    st, h, _ = raw(demo, "GET", "/try/api/state", {"Origin": ok})
    assert st == 200 and "access-control-allow-origin" not in h


def test_세션은_쿠키_없이_헤더나_쿼리로도_이어진다(cors_demo):
    """정적 호스팅에서 온 방문자는 제3자 쿠키가 막힐 수 있다 — 상태 응답의 session 을 헤더로 되돌려 준다."""
    st, h, body = raw(cors_demo, "GET", "/try/api/state")
    first = json.loads(body)
    tok = first["session"]
    assert tok and h.get("set-cookie", "").startswith("nabi_session=")
    ready = next(d for d in first["docs"] if d["name"] == "급여 3월 최종(2).xlsx")
    # 헤더로 같은 샌드박스에 승인
    st, _, body = raw(cors_demo, "POST", f"/try/api/docs/{ready['id']}/approve", {"X-Nabi-Session": tok}, {})
    assert st == 200, body
    st, _, body = raw(cors_demo, "GET", "/try/api/state", {"X-Nabi-Session": tok})
    same = json.loads(body)
    assert same["session"] == tok and same["moves"] == 1
    # 쿼리로도
    st, _, body = raw(cors_demo, "GET", f"/try/api/state?session={tok}")
    assert json.loads(body)["moves"] == 1
    # 아무것도 없으면 새 방문자
    st, _, body = raw(cors_demo, "GET", "/try/api/state")
    other = json.loads(body)
    assert other["session"] != tok and other["moves"] == 0
    # 모르는 토큰이면 새로 발급하고 응답에 알려 준다
    st, h, body = raw(cors_demo, "GET", "/try/api/state", {"X-Nabi-Session": "nope"})
    fresh = json.loads(body)
    assert fresh["session"] not in ("nope", tok) and h.get("set-cookie", "").startswith("nabi_session=")


# ---------------------------------------------------------------- 로컬 모드

def test_로컬_모드는_작업공간_하나를_쓰고_스캔이_큐를_채운다(tmp_path):
    ws = Workspace(tmp_path / "nabi")
    ws.init()
    ws.write_registry(REG)
    shutil.copy(SRC / "급여 3월 최종(2).xlsx", ws.inbox / "급여 3월 최종(2).xlsx")
    cfg = Config(site_dir=SITE, mode="local", workspace=ws.root, labeler=fake_labeler, health=lambda: True)
    srv = start(cfg)
    try:
        v = Visitor(srv)
        st = v.state()
        assert st["mode"] == "local" and st["docs"] == []
        st_code, r = v.req("POST", "/try/api/scan", {})
        assert st_code == 200 and r["scanned"] == 1, r
        d = v.doc(v.state(), "급여 3월 최종(2).xlsx")
        assert d["state"] == "ready" and d["text_head"]
        assert v.req("POST", "/try/api/reset", {})[0] == 404      # 로컬엔 샌드박스가 없다
        assert v.req("POST", f"/try/api/docs/{d['id']}/approve", {})[1]["ok"]
        assert (ws.organized / "대성정밀공업사/2026/01_원천세_급여/대성정밀공업사_2026-04_급여대장.xlsx").exists()
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- 기록(record)

def test_기록은_샘플마다_엔진_행과_본문_머리와_정답을_남긴다(samples):
    rec = build_recorded(samples_dir=samples, labels=LABELS, registry=REG, labeler=fake_labeler,
                         meta={"device": "테스트 기기", "model": "fake", "model_file": "없음", "threads": 1})
    assert rec["meta"]["device"] == "테스트 기기" and rec["meta"]["recorded_at"]
    assert [d["sample_id"] for d in rec["docs"]] == ["d07", "d16", "d01"]
    d = rec["docs"][0]
    assert d["file"] == "급여 3월 최종(2).xlsx" and d["kind"] == "엑셀"
    assert d["row"]["state"] == "ready" and d["row"]["target"]
    assert d["text_head"] and len(d["text_head"]) <= 800
    assert d["want"] == {"client": "대성정밀공업사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-04"}
    assert rec["docs"][2]["kind"] == "스캔"
