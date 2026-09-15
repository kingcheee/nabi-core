#!/usr/bin/env python3
"""규칙엔진 — 모델이 낸 라벨을 결정론으로 재판정한다 (ADR-0007).

모델은 「문서 이해」만 한다. 여기서 하는 일은 전부 규칙이다:

  - 분류(category)는 서류 종류의 함수라 **규칙이 파생해 덮어쓴다**. 모델 값이 어긋났다는 사실은 보류 사유로 남긴다.
  - 기간(period)은 서류 종류별 주기(월·분기·반기·연·상비)로 형식을 다시 잰다.
  - 거래처(client)는 대표가 등록한 목록·별칭과 대조한다. 오타는 근사 매칭, 애매하면 보류.
  - 확신이 낮으면 「보류」가 정식 출력이다. 틀린 라벨의 비용이 보류의 비용보다 크다.

같은 입력이면 같은 판정이 나온다 — 난수도, 시간도, 외부 호출도 쓰지 않는다.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from .labels import CATEGORY, CYCLE, CYCLE_RE, DOC_TYPES, norm_client, norm_period, target_path

CLIENT_MIN = 0.72        # 근사 매칭 최저 점수. 이 밑은 미등록으로 본다.
CLIENT_MARGIN = 0.08     # 1등과 2등의 점수 차. 이보다 좁으면 애매하다고 본다.
# OCR 경유 문서가 이보다 짧으면 읽기 실패로 본다.
# 2026-09-10 실측으로 잡은 값 — 카톡 사진 1건을 해상도·압축을 낮춰 4단계로 구워 OCR 했다(원자료 `bench/ocr-calibration.md`):
#   1280px/q70 310자 · 640px/q40 263자 · 400px/q25 47자 · 240px/q15 6자.
# 즉 **글자 수**가 판독 실패를 가른다. 한글 비율은 쓰지 않는다 — 통장내역(0.18)·팩스 거래명세서(0.21)처럼
# 멀쩡한데 숫자가 많은 서류가 그대로 걸려 오탐이 났다(같은 날 22건 실측).
OCR_MIN_CHARS = 120
BIZNO_RE = re.compile(r"\b(\d{3})-?(\d{2})-?(\d{5})\b")

# 프롬프트(labels.py INSTRUCTION)의 출력 예시. 모델이 이걸 통째로 베껴 내면 답이 아니라 무응답이다.
# 2026-09-10 실측 — 카톡 사진 사업자등록증(d02)이 잘 안 읽히자 MiniCPM5 가 이 네 값을 그대로 냈다.
EXAMPLE_PRED = {"client": "가상상사", "category": "정기증빙", "doc_type": "급여대장", "period": "2026-03"}

REASON_TEXT = {
    "doc_type_unknown": "서류 종류가 15종 어휘 밖이다",
    "category_mismatch": "모델이 고른 분류가 서류 종류에서 파생한 분류와 다르다",
    "period_format": "기간을 정규화하지 못했다",
    "period_cycle": "기간 형식이 이 서류의 주기와 맞지 않다",
    "client_unregistered": "거래처가 등록 목록에 없다",
    "client_ambiguous": "거래처 후보 둘이 비슷해 어느 쪽인지 못 정한다",
    "ocr_low_signal": f"OCR로 뽑은 글자가 {OCR_MIN_CHARS}자도 안 된다 — 사진이 읽히지 않았다",
    "ocr_digit_conflict": "OCR 본문의 사업자번호가 서로 달라 숫자가 흔들렸다",
    "example_echo": "모델이 프롬프트의 출력 예시를 그대로 베꼈다 — 답을 못 찾은 것이다",
}


# ---------------------------------------------------------------- 거래처 목록
@dataclass
class ClientMatch:
    name: str | None
    score: float
    how: str          # exact | alias | fuzzy | none
    runner_up: float = 0.0


@dataclass
class Registry:
    """대표가 등록한 거래처 목록과 별칭. 사무소마다 다르고, 확인 큐의 수정이 여기로 돌아온다."""
    clients: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)

    def match_client(self, raw: str) -> ClientMatch:
        q = norm_client(raw)
        if not q:
            return ClientMatch(None, 0.0, "none")

        by_norm = {norm_client(c): c for c in self.clients}
        if q in by_norm:
            return ClientMatch(by_norm[q], 1.0, "exact")
        for a, full in self.aliases.items():
            if q == norm_client(a):
                return ClientMatch(full, 1.0, "alias")

        # 부분 포함은 근사보다 먼저 본다 — '모모스토어' ⊂ '주식회사 모모스토어'
        contained = [c for n, c in by_norm.items() if len(q) >= 3 and (q in n or n in q)]
        if len(contained) == 1:
            return ClientMatch(contained[0], 0.95, "fuzzy")

        scored = sorted(((difflib.SequenceMatcher(None, q, n).ratio(), c) for n, c in by_norm.items()), reverse=True)
        if not scored:
            return ClientMatch(None, 0.0, "none")
        top, second = scored[0], (scored[1] if len(scored) > 1 else (0.0, None))
        if top[0] < CLIENT_MIN:
            return ClientMatch(None, top[0], "none", second[0])
        return ClientMatch(top[1], top[0], "fuzzy", second[0])


# ---------------------------------------------------------------- 기간
def check_period(doc_type: str, period_raw: str) -> list[str]:
    """빈 리스트면 통과. 아니면 보류 사유."""
    cycle = CYCLE.get(doc_type)
    if cycle is None:
        return []                                  # 종류를 모르면 종류 쪽 사유로 이미 잡혔다
    norm = norm_period(period_raw)
    if not norm:
        return ["period_format"]
    if not re.match(r"^(PERMANENT|\d{4}(-(0[1-9]|1[0-2]|Q[1-4]|[12]H))?)$", norm):
        return ["period_format"]
    if not CYCLE_RE[cycle].match(norm):
        return ["period_cycle"]
    return []


def derive_category(doc_type: str) -> str | None:
    return CATEGORY.get(doc_type)


# ---------------------------------------------------------------- OCR 품질
def hangul_ratio(text: str) -> float:
    letters = [ch for ch in text if not ch.isspace()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if "가" <= ch <= "힣") / len(letters)


def check_ocr(text: str) -> list[str]:
    """스캔·팩스·카톡 사진 경유 본문의 신뢰도. 텍스트 원본에는 적용하지 않는다.

    잡는 것: OCR이 거의 아무것도 못 읽은 경우, 그리고 사업자번호가 서로 어긋난 경우.
    ⚠ 못 잡는 것: 글자 수는 멀쩡한데 **내용이 틀린** 경우(640px/q40 실측 — 263자가 나오는데 한글이 숫자·영문으로
       오인식됐다). 이건 규칙으로 못 잡고 거래처 목록 대조가 걸러낸다(상호가 깨지면 client_unregistered).
       그래서 등록 목록 대조가 OCR 경로의 진짜 안전장치다.
    """
    out = []
    if len(text.strip()) < OCR_MIN_CHARS:
        out.append("ocr_low_signal")
    nums = {"".join(m.groups()) for m in BIZNO_RE.finditer(text)}
    if len(nums) >= 3:
        out.append("ocr_digit_conflict")
    return out


# ---------------------------------------------------------------- 판정
@dataclass
class Verdict:
    pred: dict                    # 모델이 낸 값 원본 — 보류여도 버리지 않는다
    client: str | None            # 규칙이 붙인 등록 거래처명
    client_score: float
    client_how: str
    category: str | None          # 규칙이 파생한 분류 (모델 값을 덮어쓴다)
    doc_type: str | None
    period: str | None            # 정규화된 기간
    held: bool                    # True = 보류. 확인 큐에서 사람이 본다.
    reasons: list[str]
    target: str | None            # 확정일 때만 채운다

    def explain(self) -> list[str]:
        return [REASON_TEXT.get(r, r) for r in self.reasons]


def decide(pred: dict, registry: Registry, text: str, scanned: bool, ext: str) -> Verdict:
    reasons: list[str] = []

    doc_type = (pred.get("doc_type") or "").strip()
    if doc_type not in DOC_TYPES:
        reasons.append("doc_type_unknown")
        doc_type = None

    category = derive_category(doc_type) if doc_type else None
    if doc_type and (pred.get("category") or "").strip() != category:
        reasons.append("category_mismatch")

    period = norm_period(pred.get("period", "")) if doc_type else None
    if doc_type:
        reasons += check_period(doc_type, pred.get("period", ""))

    m = registry.match_client(pred.get("client", ""))
    if m.name is None:
        reasons.append("client_unregistered")
    elif m.how == "fuzzy" and m.score - m.runner_up < CLIENT_MARGIN:
        reasons.append("client_ambiguous")

    if all((pred.get(k) or "").strip() == v for k, v in EXAMPLE_PRED.items()):
        reasons.append("example_echo")

    if scanned:
        reasons += check_ocr(text)

    held = bool(reasons)
    target = None
    if not held:
        target = target_path(m.name, category, doc_type, period, ext.lstrip(".").lower())

    return Verdict(pred=dict(pred), client=m.name, client_score=round(m.score, 3), client_how=m.how,
                   category=category, doc_type=doc_type, period=period,
                   held=held, reasons=reasons, target=target)
