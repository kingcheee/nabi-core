# 라벨링 엔진 v0

들어온 서류에 **거래처 · 분류 · 서류 종류 · 기간** 네 라벨을 붙여 확인 큐에 올리고, 사람이 승인하면
실무 표준 폴더로 옮긴다. 기획서 §3의 네 칸을 그대로 옮긴 것이다.

```
파일 → [파서] → 본문 → [소형 언어모델] → 4축 JSON → [규칙엔진] → 확정/보류 → [확인 큐] → 사람 승인 → [이동]
        결정론              문서 이해만          결정론                        사람            결정론
```

**언어모델이 하는 일은 한 칸뿐이다**(ADR-0007). 파일을 열어 글자를 뽑는 것도, 분류를 정하는 것도,
폴더 경로를 만드는 것도 규칙이 한다. 모델이 틀려도 파일이 사라지지 않는다.

## 빨리 써보기

```bash
# 0) 모델 서버를 띄운다 (무상태 세션 — 서류마다 KV 캐시를 새로 연다)
python3 -m engine serve --threads 4          # 띄우는 명령을 알려준다
llama-server -m ~/models/MiniCPM5-2B-Q4_K_M.gguf --host 127.0.0.1 --port 8097 -c 2048 -ub 128 -t 4 --no-warmup &

# 1) 작업공간을 만들고 거래처를 등록한다 (등록 목록이 규칙엔진의 안전장치다)
python3 -m engine init ~/nabi --clients "한빛나루식당,대성정밀공업사" --aliases "한빛나루=한빛나루식당"

# 2) 서류를 inbox 에 넣고 스캔한다 — 파일은 아직 움직이지 않는다
cp 서류들/* ~/nabi/inbox/
python3 -m engine scan ~/nabi

# 3) 확인 큐를 보고 승인한다
python3 -m engine list ~/nabi --held        # 보류된 것만
python3 -m engine show ~/nabi 7312a3e6      # 한 건 자세히
python3 -m engine approve ~/nabi --all-ready
python3 -m engine approve ~/nabi 60d4e4fc --client 한빛나루식당   # 보류된 것을 고쳐 승인
python3 -m engine tree ~/nabi               # 정리된 트리
python3 -m engine undo ~/nabi               # 마지막 이동 되돌리기
```

모델 없이 규칙만 시험하려면 `--offline samples/labels.json`을 준다.

## 작업공간

```
~/nabi/
  inbox/                 들어온 파일
  정리됨/                 거래처별 표준 폴더 트리
    한빛나루식당/
      00_기본서류/         한빛나루식당_사업자등록증.pdf
      2026/
        01_원천세_급여/     한빛나루식당_2026-08_4대보험취득확인서.hwpx
        02_통장_증빙/       한빛나루식당_2026-05_통장내역.xlsx
        03_부가세/          한빛나루식당_2026-Q1_카드매출매입내역.xlsx
        04_결산_조정/
  .nabi/registry.json    등록 거래처·별칭 (확인 큐의 수정이 여기로 돌아온다)
  .nabi/queue.jsonl       확인 큐 대장 — append only
  .nabi/moves.jsonl       이동 기록 — undo 가 되짚는다
```

파일명은 `[거래처]_[귀속시기]_[서류명].[확장자]`다(리서치 `tax-office-document-labels` §4.2 원문).
⚠ 기획서 §2.1 본문은 `[귀속시기]_[거래처]_[서류명]` 순서로 적혀 있어 원천과 어긋난다 — 기획서 세션이 판단할 일.

## 보류 사유

확신이 낮으면 「보류」가 정식 출력이다. 틀린 라벨의 비용이 보류의 비용보다 크다.

| 사유 | 언제 |
|---|---|
| `doc_type_unknown` | 서류 종류가 15종 어휘 밖 |
| `category_mismatch` | 모델이 고른 분류가 서류 종류에서 파생한 분류와 다르다. 사람이 고쳐서 승인하면(`approve --client/--doc-type/--period`, 화면의 「고쳐서 승인」) 분류는 서류 종류에서 다시 파생되므로 이 사유만으로 보류된 건은 종류 확인만으로 풀린다(2026-09-11) |
| `period_format` / `period_cycle` | 기간을 정규화 못 했거나, 이 서류의 주기(월·분기·반기·연·상비)와 안 맞다 |
| `client_unregistered` | 거래처가 등록 목록에 없다 (근사 매칭 0.72 미만) |
| `client_ambiguous` | 후보 1·2등 점수 차가 0.08 미만 |
| `ocr_low_signal` | OCR 로 뽑은 글자가 120자도 안 된다 → `bench/ocr-calibration.md` 로 잡은 값 |
| `ocr_digit_conflict` | OCR 본문의 사업자번호가 서로 다르다(3개 이상) |
| `example_echo` | 모델이 프롬프트의 출력 예시를 그대로 베꼈다 — 답을 못 찾은 것 |
| `parse_failed` / `model_failed` | 파서·모델이 실패했다 |

## 모듈

| 파일 | 무엇 |
|---|---|
| `labels.py` | 도메인 어휘 정본 — 15종·분류 3종·주기·폴더 규칙·스키마·프롬프트·정규화 |
| `extract.py` | 파서 — PDF(텍스트/스캔) · JPG/PNG · XLSX/XLS · DOCX · HWPX/HWP · CSV · TXT. OCR 은 로컬 Tesseract |
| `label.py` | llama-server `/completion` 호출. `json_schema` 로 4축 JSON 강제, `temperature=0`, `cache_prompt=false` |
| `rules.py` | 규칙엔진 — 분류 파생·기간 재검증·거래처 목록 대조·보류 판정·경로 생성 |
| `store.py` | 작업공간·확인 큐·이동 기록. 대장은 append-only, 이동은 되돌릴 수 있다 |
| `pipeline.py` | 스캔·승인·거절 |
| `__main__.py` | CLI |

## 테스트

```bash
python3 -m pytest tests/ -q
```

`tests/test_drift.py`는 **`bench/label_one.py`와 어휘·프롬프트·정규화가 같은지** 검사한다.
그 파일은 폰·N100 에 복사돼 도는 현장 코드라 의존성 0으로 자립시켜 뒀고, 그래서 두 벌이 존재한다.
한쪽만 고치면 이 테스트가 깨진다 — **프롬프트를 고치면 이미 잰 정확도는 다시 재야 한다.**

## 정확도

```bash
python3 bench/label_all.py            # 샘플 22건 전건 → bench/results/정확도-*.md
```

축별 정확도를 **모델 원값 / 규칙 통과 후**로 나눠 내고, 텍스트 원본과 OCR 경유를 따로 집계하며,
**자신있는 오탐**(확정으로 내놨는데 틀린 비율)을 잰다. 이 수가 낮아야 확인 큐가 의미가 있다.
