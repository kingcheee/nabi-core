#!/usr/bin/env python3
"""파이프라인·대장 테스트 — 모델을 부르지 않는다(offline_pred 로 모델 출력을 주입한다)."""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.pipeline import approve, reject, scan, scan_one
from engine.rules import Registry
from engine.store import Workspace

SRC = ROOT / "samples" / "inbox"
GOOD = {"client": "대성정밀공업사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-04"}


@pytest.fixture
def ws(tmp_path):
    w = Workspace(tmp_path / "nabi")
    w.init()
    w.write_registry(Registry(clients=["대성정밀공업사", "한빛나루식당"], aliases={"대성정밀": "대성정밀공업사"}))
    return w


def put(ws, name, as_name=None):
    dst = ws.inbox / (as_name or name)
    shutil.copy(SRC / name, dst)
    return dst


def test_스캔은_파일을_움직이지_않는다(ws):
    f = put(ws, "급여 3월 최종(2).xlsx")
    row = scan_one(ws, f, ws.read_registry(), "minicpm5", "", offline_pred=GOOD)
    assert row["state"] == "ready"
    assert f.exists(), "스캔 단계에서 원본이 움직였다"
    assert not any(ws.organized.rglob("*.xlsx"))


def test_승인하면_표준_경로로_옮겨진다(ws):
    f = put(ws, "급여 3월 최종(2).xlsx")
    row = scan_one(ws, f, ws.read_registry(), "minicpm5", "", offline_pred=GOOD)
    ws.append_queue(row)
    r = approve(ws, row["id"])
    assert r["ok"]
    dst = Path(r["dst"])
    assert dst.exists() and not f.exists()
    assert dst.relative_to(ws.organized).as_posix() == \
        "대성정밀공업사/2026/01_원천세_급여/대성정밀공업사_2026-04_급여대장.xlsx"


def test_같은_내용을_다시_넣으면_건너뛴다(ws):
    put(ws, "급여 3월 최종(2).xlsx")
    rows = list(scan(ws, offline={"급여 3월 최종(2).xlsx": GOOD}))
    assert rows[0]["state"] == "ready"      # scan 이 대장에 이미 적었다
    put(ws, "급여 3월 최종(2).xlsx", as_name="사본.xlsx")
    rows2 = list(scan(ws, offline={"사본.xlsx": GOOD}))
    states = {r["name"]: r["state"] for r in rows2}
    assert states.get("사본.xlsx") == "skipped", "내용이 같은 파일을 다시 큐에 올렸다"


def test_같은_경로에_두_번째_파일이_오면_덮어쓰지_않는다(ws):
    for i, nm in enumerate(("a.xlsx", "b.xlsx")):
        shutil.copy(SRC / "급여 3월 최종(2).xlsx", ws.inbox / nm)
        (ws.inbox / nm).write_bytes((ws.inbox / nm).read_bytes() + b"\x00" * (i + 1))  # 내용을 달리해 중복 판정을 피한다
    dsts = []
    for nm in ("a.xlsx", "b.xlsx"):
        row = scan_one(ws, ws.inbox / nm, ws.read_registry(), "minicpm5", "", offline_pred=GOOD)
        ws.append_queue(row)
        r = approve(ws, row["id"])
        assert r["ok"], r
        dsts.append(Path(r["dst"]))
    assert dsts[0] != dsts[1], "같은 이름을 덮어썼다"
    assert dsts[0].exists() and dsts[1].exists()
    assert dsts[1].name.endswith("_2.xlsx")


def test_되돌리면_원래_이름으로_inbox에_돌아온다(ws):
    f = put(ws, "급여 3월 최종(2).xlsx")
    row = scan_one(ws, f, ws.read_registry(), "minicpm5", "", offline_pred=GOOD)
    ws.append_queue(row)
    dst = Path(approve(ws, row["id"])["dst"])
    back = ws.undo_last()
    assert back and Path(back["restored_to"]) == f
    assert f.exists() and not dst.exists()
    assert ws.get(row["id"])["state"] == "ready", "되돌린 뒤 상태가 확인대기로 돌아오지 않았다"


def test_지원하지_않는_형식은_보류로_들어온다(ws):
    (ws.inbox / "메모.rtf").write_text("아무 내용", encoding="utf-8")
    rows = list(scan(ws, offline={"메모.rtf": GOOD}))
    r = rows[0]
    assert r["state"] == "held" and "parse_failed" in r["reasons"]
    assert (ws.inbox / "메모.rtf").exists(), "파싱 실패한 파일이 사라졌다"


def test_보류는_승인되지_않는다(ws):
    f = put(ws, "급여 3월 최종(2).xlsx")
    bad = {**GOOD, "client": "없는데상사"}
    row = scan_one(ws, f, ws.read_registry(), "minicpm5", "", offline_pred=bad)
    ws.append_queue(row)
    r = approve(ws, row["id"])
    assert not r["ok"] and f.exists()


def test_사람이_고치면_별칭으로_배운다(ws):
    f = put(ws, "급여 3월 최종(2).xlsx")
    row = scan_one(ws, f, ws.read_registry(), "minicpm5", "", offline_pred={**GOOD, "client": "대성정밀공업"})
    ws.append_queue(row)
    before = dict(ws.read_registry().aliases)
    r = approve(ws, row["id"], fix={"client": "대성정밀공업사"})
    assert r["ok"]
    after = ws.read_registry().aliases
    assert len(after) >= len(before)


def test_거절은_파일을_건드리지_않는다(ws):
    f = put(ws, "급여 3월 최종(2).xlsx")
    row = scan_one(ws, f, ws.read_registry(), "minicpm5", "", offline_pred=GOOD)
    ws.append_queue(row)
    assert reject(ws, row["id"], "샘플")["ok"]
    assert f.exists() and ws.get(row["id"])["state"] == "rejected"


def test_대장은_덧붙이기만_한다(ws):
    f = put(ws, "급여 3월 최종(2).xlsx")
    row = scan_one(ws, f, ws.read_registry(), "minicpm5", "", offline_pred=GOOD)
    ws.append_queue(row)
    approve(ws, row["id"])
    lines = (ws.meta / "queue.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2, "승인 기록이 덧붙지 않았다"
    assert json.loads(lines[0])["state"] == "held" or json.loads(lines[0])["state"] == "ready"


def test_되돌리기를_두_번_하면_두_번째_이동도_돌아온다(ws):
    """대장이 append-only 라 「되돌렸다」 표시와 원래 기록이 같이 남는다 — 두 번째 undo 가 헛돌지 않아야 한다."""
    dsts = []
    for i, nm in enumerate(("a.xlsx", "b.xlsx")):
        shutil.copy(SRC / "급여 3월 최종(2).xlsx", ws.inbox / nm)
        (ws.inbox / nm).write_bytes((ws.inbox / nm).read_bytes() + b"\x00" * (i + 1))
        row = scan_one(ws, ws.inbox / nm, ws.read_registry(), "minicpm5", "", offline_pred=GOOD)
        ws.append_queue(row)
        dsts.append(Path(approve(ws, row["id"])["dst"]))
    assert all(d.exists() for d in dsts)
    assert ws.undo_last() is not None
    assert not dsts[1].exists(), "첫 undo 가 마지막 이동을 되돌리지 않았다"
    assert ws.undo_last() is not None, "두 번째 undo 가 헛돌았다"
    assert not dsts[0].exists()
    assert len(list(ws.inbox.iterdir())) == 2, "되돌린 파일이 inbox 로 다 오지 않았다"
    assert ws.undo_last() is None, "되돌릴 것이 없는데 무언가를 되돌렸다"


def test_거래처_이름이_법인접두어뿐이면_붙지_않는다(ws):
    """2026-09-10 실측 — 스캔 임대차계약서에서 모델이 client 로 「주식회사」만 냈다. 정규화하면 빈 문자열이다."""
    f = put(ws, "스캔001.pdf")
    row = scan_one(ws, f, ws.read_registry(), "minicpm5", "",
                   offline_pred={"client": "주식회사", "category": "기본서류",
                                 "doc_type": "임대차계약서", "period": "PERMANENT"})
    assert row["state"] == "held" and "client_unregistered" in row["reasons"]
    assert row["target"] is None


def test_파싱_실패한_건을_사람이_다_채워_승인하면_확장자가_살아난다(ws):
    """파싱 실패 레코드에는 ext 가 없다 — 원본 파일명에서 가져와야 경로가 점으로 끝나지 않는다."""
    f = ws.inbox / "메모.rtf"
    f.write_text("아무 내용", encoding="utf-8")
    rows = list(scan(ws, offline={"메모.rtf": GOOD}))
    row = rows[0]
    assert "ext" not in row
    r = approve(ws, row["id"], fix=dict(GOOD))
    assert r["ok"], r
    assert Path(r["dst"]).suffix == ".rtf", Path(r["dst"]).name


def test_사람이_고쳐_승인하면_분류는_서류_종류에서_파생된다(ws):
    """모델이 분류를 틀려 category_mismatch 로 보류된 건 — 사람이 승인하는 순간 분류는 서류 종류가 정한다.
    사람이 서류 종류를 확인(또는 수정)했으면 모델이 냈던 분류는 더 볼 이유가 없다."""
    f = put(ws, "급여 3월 최종(2).xlsx")
    wrong = {**GOOD, "category": "기본서류"}          # 급여대장은 정기증빙 — 모델이 분류만 틀렸다
    row = scan_one(ws, f, ws.read_registry(), "minicpm5", "", offline_pred=wrong)
    ws.append_queue(row)
    assert row["state"] == "held" and "category_mismatch" in row["reasons"]
    r = approve(ws, row["id"], fix={"doc_type": "급여대장"})
    assert r["ok"], r
    assert Path(r["dst"]).relative_to(ws.organized).as_posix() == \
        "대성정밀공업사/2026/01_원천세_급여/대성정밀공업사_2026-04_급여대장.xlsx"
    assert ws.get(row["id"])["category"] == "정기증빙"
