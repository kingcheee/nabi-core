#!/usr/bin/env python3
"""규칙엔진 테스트 — 결정론이라 모델 없이 전부 돈다. `python3 -m pytest tests/ -q`"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import rules
from engine.labels import target_path

REGISTRY = ["한빛나루식당", "대성정밀공업사", "주식회사 모모스토어", "푸른솔외국어학원", "세진건재상사"]
ALIASES = {"한빛나루": "한빛나루식당", "대성정밀": "대성정밀공업사", "모모스토어": "주식회사 모모스토어",
           "푸른솔": "푸른솔외국어학원", "세진건재": "세진건재상사"}


def reg():
    return rules.Registry(clients=REGISTRY, aliases=ALIASES)


# ---------------------------------------------------------------- 거래처 대조
def test_거래처_정확히_일치():
    m = reg().match_client("대성정밀공업사")
    assert m.name == "대성정밀공업사" and m.score == 1.0 and m.how == "exact"


def test_거래처_등록된_약칭():
    m = reg().match_client("대성정밀")
    assert m.name == "대성정밀공업사" and m.how == "alias"


def test_거래처_법인_접두어는_무시하고_일치():
    """모델이 '모모스토어'라 내놓아도 '주식회사 모모스토어'로 붙어야 한다."""
    m = reg().match_client("모모스토어")
    assert m.name == "주식회사 모모스토어"


def test_거래처_오타는_근사로_붙는다():
    m = reg().match_client("한빛나로식당")
    assert m.name == "한빛나루식당" and m.how == "fuzzy" and m.score >= rules.CLIENT_MIN


def test_거래처_미등록은_안_붙는다():
    m = reg().match_client("없는상사")
    assert m.name is None and m.score < rules.CLIENT_MIN


def test_거래처_빈값은_안_붙는다():
    assert reg().match_client("").name is None


# ---------------------------------------------------------------- 기간 검증
def test_기간_주기별_형식():
    assert rules.check_period("급여대장", "2026-03") == []
    assert rules.check_period("사업자등록증", "PERMANENT") == []
    assert rules.check_period("거래명세서", "2026-Q2") == []
    assert rules.check_period("부가세신고서", "2026-1H") == []
    assert rules.check_period("잔액증명서", "2025") == []


def test_기간_주기가_어긋나면_잡는다():
    """부가세신고서에 월을 적어 오면 규칙이 잡아야 한다 — 모델이 자주 틀리는 자리."""
    assert "period_cycle" in rules.check_period("부가세신고서", "2026-07")
    assert "period_cycle" in rules.check_period("급여대장", "2026-Q1")
    assert "period_cycle" in rules.check_period("사업자등록증", "2026-03")


def test_기간_형식이_깨지면_잡는다():
    assert "period_format" in rules.check_period("급여대장", "삼월")
    assert "period_format" in rules.check_period("급여대장", "")


def test_기간_월은_13월을_거부한다():
    assert rules.check_period("급여대장", "2026-13") != []


# ---------------------------------------------------------------- 분류 파생
def test_분류는_서류종류에서_파생된다():
    assert rules.derive_category("급여대장") == "정기증빙"
    assert rules.derive_category("부가세신고서") == "신고증빙"
    assert rules.derive_category("임대차계약서") == "기본서류"


def test_모델이_분류를_틀리면_신호로_남는다():
    """분류는 규칙이 덮어쓰되, 어긋났다는 사실은 보류 사유로 기록된다."""
    v = rules.decide({"client": "대성정밀", "category": "기본서류", "doc_type": "급여대장", "period": "2026-03"},
                     reg(), text="급여대장 본문 " * 30, scanned=False, ext="xlsx")
    assert v.category == "정기증빙"
    assert "category_mismatch" in v.reasons
    assert v.held is True


# ---------------------------------------------------------------- 판정
def test_전부_맞으면_확정되고_경로가_나온다():
    v = rules.decide({"client": "대성정밀", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-03"},
                     reg(), text="급여대장 본문 " * 30, scanned=False, ext="xlsx")
    assert v.held is False and v.reasons == []
    assert v.target == target_path("대성정밀공업사", "정기증빙", "급여대장", "2026-03", "xlsx")


def test_서류종류가_어휘_밖이면_보류():
    v = rules.decide({"client": "대성정밀", "category": "정기증빙", "doc_type": "세금계산서같은것", "period": "2026-03"},
                     reg(), text="본문 " * 50, scanned=False, ext="pdf")
    assert v.held and "doc_type_unknown" in v.reasons and v.target is None


def test_미등록_거래처는_보류되고_경로를_만들지_않는다():
    v = rules.decide({"client": "없는상사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-03"},
                     reg(), text="본문 " * 50, scanned=False, ext="xlsx")
    assert v.held and "client_unregistered" in v.reasons and v.target is None


def test_OCR_경유_텍스트가_너무_짧으면_보류():
    """스캔·팩스·카톡 사진이 제대로 안 읽힌 경우 — 숫자가 흔들리기 전에 글자 자체가 없다."""
    v = rules.decide({"client": "한빛나루", "category": "기본서류", "doc_type": "사업자등록증", "period": "PERMANENT"},
                     reg(), text="사업자 등록", scanned=True, ext="pdf")
    assert v.held and "ocr_low_signal" in v.reasons


def test_텍스트_문서는_짧아도_OCR_사유가_붙지_않는다():
    v = rules.decide({"client": "한빛나루", "category": "기본서류", "doc_type": "사업자등록증", "period": "PERMANENT"},
                     reg(), text="사업자 등록", scanned=False, ext="pdf")
    assert "ocr_low_signal" not in v.reasons


def test_OCR_경유_사업자번호가_서로_다르면_보류():
    """카톡 사진은 8↔0·3↔8 오인식이 잦다 — 같은 서류에 사업자번호가 여러 개면 흔들린 것이다."""
    t = "공급받는자 999-81-00011 " + ("본문 " * 60) + " 999-81-00011 " + " 999-81-00018 " + " 999-81-00071 "
    v = rules.decide({"client": "한빛나루", "category": "정기증빙", "doc_type": "종이세금계산서", "period": "2026-03"},
                     reg(), text=t, scanned=True, ext="jpg")
    assert v.held and "ocr_digit_conflict" in v.reasons


def test_같은_사업자번호가_여러번_나오는_것은_정상():
    t = "공급받는자 999-81-00011 " + ("본문 " * 60) + " 999-81-00011 " + " 999-81-00011 "
    v = rules.decide({"client": "한빛나루", "category": "정기증빙", "doc_type": "종이세금계산서", "period": "2026-03"},
                     reg(), text=t, scanned=True, ext="jpg")
    assert "ocr_digit_conflict" not in v.reasons


def test_같은_입력이면_같은_판정():
    """ADR-0007 — 같은 파일을 넣으면 같은 라벨이 나온다."""
    args = ({"client": "세진건재", "category": "신고증빙", "doc_type": "거래명세서", "period": "2026-Q1"},
            reg())
    kw = dict(text="거래명세서 본문 " * 30, scanned=False, ext="xlsx")
    a, b = rules.decide(*args, **kw), rules.decide(*args, **kw)
    assert (a.held, a.reasons, a.target, a.client) == (b.held, b.reasons, b.target, b.client)


def test_보류여도_라벨은_남는다():
    """틀려도 파일이 사라지지 않는다 — 보류 판정에도 모델이 낸 값은 그대로 보존한다."""
    v = rules.decide({"client": "없는상사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-03"},
                     reg(), text="본문 " * 50, scanned=False, ext="xlsx")
    assert v.pred["client"] == "없는상사" and v.doc_type == "급여대장" and v.period == "2026-03"


# ---------------------------------------------------------------- 예시 베끼기
def test_프롬프트_예시를_그대로_베낀_출력은_보류():
    """2026-09-10 실측 — 카톡 사진이 잘 안 읽히면 MiniCPM5 가 프롬프트의 출력 예시를 그대로 냈다.
    답이 아니라 무응답이므로 확정으로 내보내면 안 된다."""
    v = rules.decide(dict(rules.EXAMPLE_PRED), reg(), text="본문 " * 80, scanned=True, ext="jpg")
    assert v.held and "example_echo" in v.reasons


def test_예시와_한_칸만_달라도_베끼기로_보지_않는다():
    pred = dict(rules.EXAMPLE_PRED) | {"client": "세진건재"}
    v = rules.decide(pred, reg(), text="본문 " * 80, scanned=False, ext="xlsx")
    assert "example_echo" not in v.reasons
