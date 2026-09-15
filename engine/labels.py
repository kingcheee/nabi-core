#!/usr/bin/env python3
"""도메인 어휘 정본 — 서류 종류 15종·분류 3종·폴더 규칙·기간 정규화·모델 프롬프트.

리서치 `2026-09-10-tax-office-document-labels` §4.2 규격이 원천이다.

⚠ `bench/label_one.py`는 폰·N100에 그대로 복사돼 돌아가는 **현장 코드라 의존성 0으로 자립**시켜 둔다.
   그래서 프롬프트·스키마·정규화가 두 곳에 있다. 두 벌이 갈라지지 않도록 `tests/test_drift.py`가 묶는다 —
   여기를 고치면 그 테스트가 깨지고, bench 쪽도 같이 고치라고 알려준다.

⚠ 파일명 순서는 `[거래처]_[귀속시기]_[서류명]`이다(리서치 §4.2 `generate_target_path` 원문).
   기획서 §2.1 본문은 `[귀속시기]_[거래처]_[서류명]`이라고 적혀 있는데 이쪽이 원천과 어긋난다 — 기획서 세션이 판단할 일.
"""
from __future__ import annotations

import json
import re

# ---------------------------------------------------------------- 어휘
DOC_TYPES = ["사업자등록증", "임대차계약서", "법인등기부등본", "통장내역", "급여대장", "근로계약서", "전자세금계산서",
             "종이세금계산서", "종이영수증", "4대보험취득확인서", "거래명세서", "카드매출매입내역", "잔액증명서",
             "원천세신고서", "부가세신고서"]

CATEGORIES = ["기본서류", "정기증빙", "신고증빙"]

# 서류 종류 → 분류. 분류는 종류의 함수다(모델이 따로 고를 값이 아니라 규칙이 파생한다).
CATEGORY = {"사업자등록증": "기본서류", "임대차계약서": "기본서류", "법인등기부등본": "기본서류",
            "통장내역": "정기증빙", "급여대장": "정기증빙", "근로계약서": "정기증빙", "전자세금계산서": "정기증빙",
            "종이세금계산서": "정기증빙", "종이영수증": "정기증빙", "4대보험취득확인서": "정기증빙",
            "거래명세서": "신고증빙", "카드매출매입내역": "신고증빙", "잔액증명서": "신고증빙",
            "원천세신고서": "신고증빙", "부가세신고서": "신고증빙"}

# 서류 종류 → 연도 폴더 아래 하위 폴더
SUB_MAP = {"급여대장": "01_원천세_급여", "근로계약서": "01_원천세_급여", "4대보험취득확인서": "01_원천세_급여",
           "원천세신고서": "01_원천세_급여",
           "통장내역": "02_통장_증빙", "전자세금계산서": "02_통장_증빙", "종이세금계산서": "02_통장_증빙",
           "종이영수증": "02_통장_증빙",
           "거래명세서": "03_부가세", "카드매출매입내역": "03_부가세", "부가세신고서": "03_부가세",
           "잔액증명서": "04_결산_조정"}

# 서류 종류 → 기간 주기. 규칙엔진이 모델이 낸 period 형식을 이것으로 재검증한다.
#   PERMANENT 상비철 · M 월 · Q 분기 · H 반기 · Y 연
CYCLE = {"사업자등록증": "PERMANENT", "임대차계약서": "PERMANENT", "법인등기부등본": "PERMANENT",
         "통장내역": "M", "급여대장": "M", "근로계약서": "M", "전자세금계산서": "M",
         "종이세금계산서": "M", "종이영수증": "M", "4대보험취득확인서": "M", "원천세신고서": "M",
         "거래명세서": "Q", "카드매출매입내역": "Q",
         "부가세신고서": "H",
         "잔액증명서": "Y"}

CYCLE_RE = {
    "PERMANENT": re.compile(r"^PERMANENT$"),
    "M": re.compile(r"^\d{4}-(0[1-9]|1[0-2])$"),
    "Q": re.compile(r"^\d{4}-Q[1-4]$"),
    "H": re.compile(r"^\d{4}-[12]H$"),
    "Y": re.compile(r"^\d{4}$"),
}

# ---------------------------------------------------------------- 모델 출력 스키마 (GBNF 로 변환돼 강제된다)
SCHEMA = {
    "type": "object",
    "properties": {
        "client": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "doc_type": {"type": "string", "enum": DOC_TYPES},
        "period": {"type": "string"},
    },
    "required": ["client", "category", "doc_type", "period"],
}

INSTRUCTION = (
    "아래는 세무·회계 사무소에 들어온 서류의 본문입니다. 다음 네 가지를 찾아 JSON 객체 하나만 출력하세요. 설명은 쓰지 마세요.\n"
    "- client: 이 서류의 주인인 고객사 상호. 세무대리인·은행·공급자·임대인이 아니라 신고인·예금주·사업장·공급받는자·임차인 쪽.\n"
    "- doc_type: 다음 중 하나: " + ", ".join(DOC_TYPES) + "\n"
    "- category: 사업자등록증·임대차계약서·법인등기부등본 → 기본서류 / 통장내역·급여대장·근로계약서·전자세금계산서·종이세금계산서·종이영수증·4대보험취득확인서 → 정기증빙 / 거래명세서·카드매출매입내역·잔액증명서·원천세신고서·부가세신고서 → 신고증빙\n"
    "- period: 기본서류는 PERMANENT. 월 단위 서류(정기증빙·원천세신고서)는 YYYY-MM. 거래명세서·카드매출매입내역은 분기 YYYY-Q1~Q4. "
    "부가세신고서는 반기 YYYY-1H 또는 YYYY-2H. 잔액증명서는 발급기준일의 연도 YYYY.\n"
    "출력 예: {\"client\": \"○○상사\", \"category\": \"정기증빙\", \"doc_type\": \"급여대장\", \"period\": \"2026-03\"}\n"
    "client 는 반드시 본문에 그대로 적힌 상호를 옮겨 적으세요. 본문에 없는 이름을 만들지 마세요.\n\n[서류 본문]\n"
)

MAX_CHARS = 3500


def build_prompt(text: str, model: str, max_chars: int = MAX_CHARS) -> str:
    body = text.strip()[:max_chars]
    p = "<|im_start|>user\n" + INSTRUCTION + body + "\n<|im_end|>\n<|im_start|>assistant\n"
    if model.startswith("minicpm"):
        p += "<think>\n\n</think>\n\n"  # 사고 모드 끄기 — 빈 사고 태그 선주입
    return p


# ---------------------------------------------------------------- 정규화
def norm_client(s: str) -> str:
    return re.sub(r"\(주\)|주식회사|㈜|\s+", "", s or "").strip()


def norm_period(s: str) -> str:
    s = (s or "").strip().upper().replace(".", "-").replace("/", "-").replace("년", "-").replace("월", "")
    if "PERMANENT" in s or "영구" in s:
        return "PERMANENT"
    m = re.match(r"^(\d{4})-?\s*(Q[1-4]|[12]H)", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"^(\d{4})-?\s*(\d{1,2})\b", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})", s)
    return m.group(1) if m else s


def parse_json(raw: str) -> dict:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    m = re.search(r"\{.*?\}", raw, flags=re.S)
    if not m:
        return {}
    for cand in (m.group(0), m.group(0).replace("'", '"')):
        try:
            return json.loads(cand)
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------- 폴더 규칙
def target_path(client: str, category: str, doc_type: str, period_norm: str, ext: str) -> str:
    """리서치 §4.2 generate_target_path 규칙 그대로. 반환은 거래처 루트부터의 상대경로."""
    if category == "기본서류" or period_norm == "PERMANENT":
        return f"{client}/00_기본서류/{client}_{doc_type}.{ext}"
    year = period_norm.split("-")[0]
    sub = SUB_MAP.get(doc_type, "99_기타")
    return f"{client}/{year}/{sub}/{client}_{period_norm}_{doc_type}.{ext}"
