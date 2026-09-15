#!/usr/bin/env python3
"""파서 — 파일에서 글자를 뽑는다. 여기까지는 LLM이 하지 않는다(ADR-0007).

지원: 텍스트 PDF · 스캔 PDF(OCR) · JPG/PNG(OCR) · XLSX · XLS · DOCX · HWPX · HWP · CSV · TXT
OCR은 로컬 Tesseract 한국어(kor+eng)다. 네트워크로 나가는 경로는 없다.

반환은 `Extracted` — 본문과 함께 **OCR을 거쳤는지**(`scanned`)를 같이 돌려준다. 규칙엔진이 이 값으로
스캔·사진 전용 보류 조건을 켠다.
"""
from __future__ import annotations

import csv
import io
import re
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

OCR_LANG = "kor+eng"
OCR_DPI = 200
TEXT_PDF_MIN = 40          # 텍스트 레이어가 이보다 짧으면 스캔본으로 보고 OCR로 넘긴다

SUPPORTED = {".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls", ".docx", ".hwpx", ".hwp", ".csv", ".txt"}


@dataclass
class Extracted:
    text: str
    scanned: bool              # OCR을 거쳤나
    ext: str                   # 원본 확장자(점 없이, 소문자)
    how: str                   # 어떤 경로로 뽑았나 — 확인 큐에 그대로 보여준다


class UnsupportedFormat(Exception):
    pass


def ocr_image(path: Path) -> str:
    r = subprocess.run(["tesseract", str(path), "-", "-l", OCR_LANG, "--psm", "6"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"tesseract 실패: {r.stderr.strip()[:200]}")
    return r.stdout


def _pdf(path: Path) -> Extracted:
    import pymupdf
    doc = pymupdf.open(path)
    txt = "\n".join(p.get_text() for p in doc)
    if len(txt.strip()) >= TEXT_PDF_MIN:
        return Extracted(txt, False, "pdf", "PDF 텍스트 레이어")
    out = []
    for i, page in enumerate(doc):
        png = path.with_suffix(f".ocr{i}.png")
        try:
            page.get_pixmap(dpi=OCR_DPI).save(png)
            out.append(ocr_image(png))
        finally:
            png.unlink(missing_ok=True)
    return Extracted("\n".join(out), True, "pdf", f"스캔 PDF → Tesseract({OCR_LANG}) {OCR_DPI}dpi")


def _xlsx(path: Path) -> Extracted:
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"[시트: {ws.title}]")
        for row in ws.iter_rows(values_only=True):
            vals = [str(v) for v in row if v is not None]
            if vals:
                lines.append("\t".join(vals))
    return Extracted("\n".join(lines), False, "xlsx", "엑셀 셀 값")


def _docx(path: Path) -> Extracted:
    import docx
    d = docx.Document(path)
    lines = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            lines.append("\t".join(c.text for c in row.cells))
    return Extracted("\n".join(lines), False, "docx", "워드 문단·표")


# HWPX 안에서 줄·칸을 나누는 태그. 태그를 그냥 지우면 본문이 한 줄로 붙는다.
# ⚠ 2026-09-10 실측 — hwp-mcp `create_hwpx_document` 로 만든 파일은 문서 전체가 `<hp:p>` **하나**이고
#   줄은 `<hp:lineBreak/>` 로만 나뉜다. 구분자 없이 지운 탓에 「…한빛나루식당사업장관리번호: …」처럼
#   필드가 다 붙어 모델이 서류 종류를 못 갈랐다(4대보험취득확인서 → 사업자등록증 오분류).
_HWPX_BREAK = re.compile(r"<hp:(lineBreak|tab)\b[^>]*/?>|</hp:p>|</hp:tc>|</hp:tr>", re.I)
_HWPX_T = re.compile(r"<hp:t[^>]*>(.*?)</hp:t>", re.S)
_XML_ESC = {"&lt;": "<", "&gt;": ">", "&amp;": "&", "&quot;": '"', "&apos;": "'", "&#13;": "\n"}


def _hwpx(path: Path) -> Extracted:
    lines: list[str] = []
    with zipfile.ZipFile(path) as z:
        for n in sorted(m for m in z.namelist() if m.startswith("Contents/section")):
            xml = z.read(n).decode("utf-8", "replace")
            xml = _HWPX_BREAK.sub("\n", xml)              # 줄·칸 구분을 먼저 개행으로 바꾼다
            body = "".join(_HWPX_T.findall(xml))          # 그 다음 글자만 모은다
            body = re.sub(r"<[^>]+>", "", body)
            for k, v in _XML_ESC.items():
                body = body.replace(k, v)
            lines += [l.rstrip() for l in body.split("\n")]
    text = "\n".join(l for l in lines if l.strip())
    return Extracted(text, False, "hwpx", "HWPX section XML (줄바꿈·표 칸 구분 복원)")


def _soffice(path: Path, ext: str) -> Extracted:
    """구버전 포맷(.hwp·.xls)은 LibreOffice 로 한 번 변환해 읽는다. 오프라인 로컬 변환이다."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(["soffice", "--headless", "--convert-to", "txt:Text (encoded):UTF8",
                            "--outdir", td, str(path)], capture_output=True, text=True, timeout=180)
        cand = list(Path(td).glob("*.txt"))
        if not cand:
            raise UnsupportedFormat(f"{ext} → txt 변환 실패: {(r.stderr or r.stdout).strip()[:200]}")
        return Extracted(cand[0].read_text(encoding="utf-8", errors="replace"), False, ext,
                         "LibreOffice 변환 → 텍스트")


def _csv(path: Path) -> Extracted:
    raw = path.read_text(encoding="utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(raw)))
    return Extracted("\n".join("\t".join(r) for r in rows if any(c.strip() for c in r)), False, "csv", "CSV 행")


def extract(path: str | Path) -> Extracted:
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise UnsupportedFormat(f"지원하지 않는 형식: {ext or '확장자 없음'} ({path.name})")
    if ext == ".pdf":
        return _pdf(path)
    if ext in (".jpg", ".jpeg", ".png"):
        return Extracted(ocr_image(path), True, ext.lstrip("."), f"사진 → Tesseract({OCR_LANG})")
    if ext == ".xlsx":
        return _xlsx(path)
    if ext == ".docx":
        return _docx(path)
    if ext == ".hwpx":
        return _hwpx(path)
    if ext in (".hwp", ".xls"):
        return _soffice(path, ext.lstrip("."))
    if ext == ".csv":
        return _csv(path)
    return Extracted(path.read_text(encoding="utf-8", errors="replace"), False, "txt", "그대로 읽음")
