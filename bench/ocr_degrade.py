#!/usr/bin/env python3
"""OCR 판독 하한 재현 — 카톡 사진 1건을 4단계로 열화시켜 Tesseract 로 읽는다. 결과는 ocr-calibration.md.

    python3 bench/ocr_degrade.py [원본.pdf]
"""
import io
import re
import subprocess
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples/originals/d02-KakaoTalk_20260305_101522.pdf"
STEPS = [(1280, 70, 0, "1280px·q70 (카톡 기본)"), (640, 40, 0.6, "640px·q40"),
         (400, 25, 1.2, "400px·q25"), (240, 15, 2.0, "240px·q15")]
MEAN = re.compile(r"[가-힣A-Za-z0-9]")
PUNCT = set(".,:;()[]%-/원년월일　")

page = pymupdf.open(SRC)[0]
print(f"원본 {SRC.name}\n")
print(f"| 조건 | 크기 | 글자 | 의미글자비 | 한글비 |\n|---|---:|---:|---:|---:|")
for w, q, blur, tag in STEPS:
    im = Image.open(io.BytesIO(page.get_pixmap(dpi=150).tobytes("png"))).convert("L")
    im = im.resize((w, int(im.height * w / im.width)), Image.LANCZOS)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    f = Path("/tmp") / f"ocrcal-{w}.jpg"
    im.save(f, quality=q)
    t = subprocess.run(["tesseract", str(f), "-", "-l", "kor+eng", "--psm", "6"],
                       capture_output=True, text=True).stdout
    ls = [c for c in t if not c.isspace()]
    meaning = sum(1 for c in ls if MEAN.match(c) or c in PUNCT) / max(1, len(ls))
    ko = sum(1 for c in ls if "가" <= c <= "힣") / max(1, len(ls))
    print(f"| {tag} | {f.stat().st_size // 1024}KB | {len(t)} | {meaning:.2f} | {ko:.2f} |")
    f.unlink(missing_ok=True)
