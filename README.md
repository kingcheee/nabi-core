# 나비 (nabi) — 사무소 서류 라벨링 비서, 온디바이스 sLLM

세무·회계 사무소에 들어오는 서류 파일에 **거래처 · 분류 · 서류 종류 · 기간** 네 라벨을 붙이고, 사람이 확인 큐에서 승인하면 실무 표준 폴더로 옮긴다. 모델은 구형 PC·폐휴대폰의 CPU에서 로컬로 돌고, 파일은 기기 밖으로 나가지 않는다.

> **대회 출품용 시제품**이다(2026 원티드 AI Championship). 정식 서비스가 아니며, 페이지·문서에 적힌 숫자는 전부 우리가 직접 잰 값이다 — 재지 않은 숫자는 적지 않는다.

## 무엇을 하나

```
파일 → [파서] → 본문 → [소형 언어모델] → 4축 JSON → [규칙엔진] → 확정/보류 → [확인 큐] → 사람 승인 → [이동]
        결정론              문서 이해만          결정론                        사람            결정론
```

- **언어모델이 하는 일은 한 칸뿐이다.** 파일을 열어 글자를 뽑는 것도, 분류를 정하는 것도, 폴더 경로를 만드는 것도 규칙이 한다. 모델이 틀려도 파일이 사라지지 않는다.
- 확신이 낮으면 **보류**가 정식 출력이다. 보류된 건은 사람이 고쳐서 승인하고, 고친 내용(거래처 별칭 등)은 규칙으로 되돌아온다.
- 서류 종류 15종(사업자등록증·임대차계약서·급여대장·근로계약서·세금계산서·거래명세서·부가세신고서 …), 형식은 PDF(텍스트·스캔)·JPG·XLSX·DOCX·HWPX·CSV·TXT.

| 폴더 | 무엇 |
|---|---|
| `engine/` | 라벨링 엔진 — 파서 · 모델 호출 · 규칙엔진 · 확인 큐 · CLI (`engine/README.md`) |
| `web/` | 로컬 웹 UI 서버 — 확인 큐 API + 체험 모드, 파이썬 표준 라이브러리만 (`web/README.md`) |
| `site/` | 제품 사이트(랜딩 · 앱 화면 · 설치 · 지원 기기) — 정적 HTML, 외부 자원 0 (`site/README.md`) |
| `bench/` | 기기 공통 실측 스크립트와 결과 — 노트북·Galaxy A34·Note 20 등 (`bench/README.md`) |
| `samples/` | 시험용 서류 22건 + 정답 라벨 — **전부 가공 데이터** (아래 참고) |
| `tests/` | pytest — 모델 없이 돈다 |
| `tools/` | 샘플 생성기, 스크린샷 스크립트 |

## 실행 방법

요구: Python 3.10+, [llama.cpp](https://github.com/ggml-org/llama.cpp)의 `llama-server`, 파이썬 패키지 `pymupdf` `openpyxl` `python-docx` `Pillow`. 스캔·사진 OCR은 로컬 Tesseract(`kor+eng`)를 쓴다(없으면 OCR 서류만 보류된다).

```bash
pip install pymupdf openpyxl python-docx Pillow pytest

# 0) 모델 서버 — 가중치는 저장소에 없다. MiniCPM5-2B Q4_K_M(Apache-2.0) 또는 Qwen2.5-1.5B-Instruct Q4_K_M(Apache-2.0)
llama-server -m ~/models/MiniCPM5-2B-Q4_K_M.gguf --host 127.0.0.1 --port 8097 -c 2048 -ub 128 -t 4 --no-warmup &

# 1) 작업공간을 만들고 거래처를 등록한다 (등록 목록이 규칙엔진의 안전장치다)
python3 -m engine init ~/nabi --clients "한빛나루식당,대성정밀공업사" --aliases "한빛나루=한빛나루식당"

# 2) 서류를 inbox 에 넣고 스캔한다 — 파일은 아직 움직이지 않는다
cp samples/inbox/* ~/nabi/inbox/
python3 -m engine scan ~/nabi

# 3) 확인 큐를 보고 승인한다
python3 -m engine list ~/nabi --held
python3 -m engine approve ~/nabi --all-ready
python3 -m engine tree ~/nabi

# 웹 화면 — 로컬 모드(설치된 PC) 또는 체험 모드(샘플 22건, 방문자별 샌드박스)
python3 -m web --workspace ~/nabi        # http://127.0.0.1:8098/try/
python3 -m web --demo
```

모델 없이 규칙만 시험하려면 `python3 -m engine scan ~/nabi --offline samples/labels.json`.

### 테스트

```bash
python3 -m pytest tests/ -q      # 모델 서버 불필요 — 모델 출력은 주입한다(파서·규칙·웹은 진짜)
```

### 실측

```bash
bench/bench.sh --quick           # 기기 하나에서 스모크 (모델은 스크립트가 받는다)
python3 bench/label_all.py       # 샘플 22건 정확도 → bench/results/정확도-*.md
```

## 샘플 서류에 대해

`samples/inbox/`의 22건은 `tools/make_samples.py`가 만든 **가공 서류**다. 가상 고객사 5곳(한빛나루식당·대성정밀공업사·주식회사 모모스토어·푸른솔외국어학원·세진건재상사)의 상호·대표자·사업자번호(`999-81-000xx`)·주소(「가상로」)·전화(`0xx-000-00xx`)·계좌·주민번호(`000000-0000000`)는 전부 지어낸 값이며 실존 개인·기업 정보는 들어 있지 않다. 파일명·약칭·기간 불일치·중복 같은 「지저분함」은 일부러 넣은 것이다.

## 라이선스

이 저장소의 코드·문서·샘플은 **Apache License 2.0**이다 — `LICENSE` 참고. Copyright 2026 Kim Jiwoo (김지우).

모델 가중치는 저장소에 포함하지 않으며 각 모델의 라이선스를 따른다(MiniCPM5-2B, Qwen2.5-1.5B-Instruct: Apache-2.0). llama.cpp는 MIT.
