#!/usr/bin/env python3
"""어휘 드리프트 방지 — `engine/labels.py`(제품)와 `bench/label_one.py`(현장 실측 코드)가 같아야 한다.

bench/label_one.py 는 폰·N100 에 그대로 복사돼 돌기 때문에 의존성 0으로 자립시켜 둔다(엔진을 import 하지 않는다).
그래서 프롬프트·스키마·정규화가 두 벌 존재한다. 한쪽만 고치면 실측값과 제품 동작이 어긋나므로 이 테스트가 잡는다.

깨지면: 두 파일을 같이 고치고, 이미 잰 실측값이 무효가 되는지 따져라(프롬프트가 바뀌면 정확도는 다시 재야 한다).
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import labels as L

spec = importlib.util.spec_from_file_location("label_one", ROOT / "bench" / "label_one.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


def test_서류종류_어휘가_같다():
    assert L.DOC_TYPES == B.DOC_TYPES


def test_분류_어휘가_같다():
    assert L.CATEGORIES == B.CATEGORIES


def test_출력_스키마가_같다():
    assert L.SCHEMA == B.SCHEMA


def test_프롬프트_지시문이_같다():
    assert L.INSTRUCTION == B.INSTRUCTION


def test_프롬프트_전체가_같다():
    for model in ("minicpm5", "qwen"):
        t = "본문 예시 " * 20
        assert L.build_prompt(t, model) == B.build_prompt(t, model, L.MAX_CHARS)


def test_기간_정규화가_같다():
    for s in ("2026-03", "2026.3", "2026년 3월", "2026-Q2", "2026-1H", "2025", "PERMANENT", "영구", "삼월", ""):
        assert L.norm_period(s) == B.norm_period(s), s


def test_거래처_정규화가_같다():
    for s in ("(주)한국상사", "주식회사 모모스토어", "㈜세진", " 한빛 나루 ", ""):
        assert L.norm_client(s) == B.norm_client(s), s


def test_JSON_파싱이_같다():
    for raw in ('{"client":"가","category":"정기증빙","doc_type":"급여대장","period":"2026-03"}',
                "<think>\n생각\n</think>\n{'client':'가'}", "설명 없음", ""):
        assert L.parse_json(raw) == B.parse_json(raw), raw


def test_분류_파생표가_어휘를_전부_덮는다():
    assert set(L.CATEGORY) == set(L.DOC_TYPES)
    assert set(L.CATEGORY.values()) <= set(L.CATEGORIES)


def test_주기표가_어휘를_전부_덮는다():
    assert set(L.CYCLE) == set(L.DOC_TYPES)
    assert set(L.CYCLE.values()) <= set(L.CYCLE_RE)


def test_하위폴더표는_기본서류를_뺀_전부를_덮는다():
    need = {d for d in L.DOC_TYPES if L.CATEGORY[d] != "기본서류"}
    assert need <= set(L.SUB_MAP), need - set(L.SUB_MAP)


def test_샘플_정답라벨이_어휘_안에_있다():
    import json
    for row in json.loads((ROOT / "samples" / "labels.json").read_text(encoding="utf-8")):
        assert row["doc_type"] in L.DOC_TYPES, row["file"]
        assert row["category"] == L.CATEGORY[row["doc_type"]], row["file"]
        assert L.CYCLE_RE[L.CYCLE[row["doc_type"]]].match(row["period"]["normalized"]), row["file"]


def test_샘플_정답경로가_엔진_규칙과_같다():
    """make_samples.py 가 만든 target_path 와 엔진의 규칙이 같은 답을 내야 한다."""
    import json
    for row in json.loads((ROOT / "samples" / "labels.json").read_text(encoding="utf-8")):
        got = L.target_path(row["client"], row["category"], row["doc_type"], row["period"]["normalized"], row["format"])
        assert got == row["target_path"], (row["file"], got, row["target_path"])
