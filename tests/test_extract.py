#!/usr/bin/env python3
"""파서 테스트 — 샘플 실물로 돈다(모델 불필요)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.extract import SUPPORTED, UnsupportedFormat, extract

INBOX = ROOT / "samples" / "inbox"


def test_지원하지_않는_형식은_명확히_거부한다():
    with pytest.raises(UnsupportedFormat):
        extract(INBOX.parent / "labels.json")   # .json 은 지원 목록 밖


def test_샘플_22건_전부_글자가_나온다():
    files = sorted(p for p in INBOX.iterdir() if p.is_file())
    assert len(files) == 22
    for p in files:
        e = extract(p)
        assert len(e.text.strip()) > 100, (p.name, len(e.text))


def test_OCR을_거친_건만_scanned가_참이다():
    import json
    want = {r["file"]: r["scanned"] for r in json.loads((ROOT / "samples" / "labels.json").read_text(encoding="utf-8"))}
    for p in sorted(INBOX.iterdir()):
        if p.name in want:
            assert extract(p).scanned == want[p.name], p.name


def test_hwpx_는_줄바꿈을_살린다():
    """2026-09-10 실측 — hwp-mcp 로 만든 hwpx 는 문서 전체가 <hp:p> 하나이고 줄은 <hp:lineBreak/> 다.
    태그를 구분자 없이 지우면 본문이 한 줄로 붙어 모델이 필드를 못 가른다."""
    e = extract(INBOX / "취득확인.hwpx")
    assert e.text.count("\n") >= 5, f"줄바꿈 {e.text.count(chr(10))}개뿐 — 다 붙었다"
    assert "한빛나루식당" in e.text
    # 「사업장명칭: 한빛나루식당」 다음이 새 줄이어야 한다
    assert "한빛나루식당사업장관리번호" not in e.text


def test_hwpx_표는_셀이_구분된다():
    e = extract(INBOX / "취득확인.hwpx")
    assert "박지훈" in e.text


def test_지원_목록에_실무_형식이_다_있다():
    for ext in (".pdf", ".jpg", ".png", ".xlsx", ".xls", ".docx", ".hwpx", ".hwp", ".csv", ".txt"):
        assert ext in SUPPORTED
