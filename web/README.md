# 웹 — 정적 사이트 + 확인 큐 API (파이썬 표준 라이브러리만)

기획서 §5.1 「한 도메인 네 경로」의 서버 쪽이다. **앱 하나, 모드 둘.**

| 모드 | 명령 | 무엇 |
|---|---|---|
| 체험 | `python3 -m web --demo` | `/try` — 샘플 22건만 · 업로드 없음 · **방문자별 샌드박스** · 큐는 미리 잰 결과로 즉시 채움 · 「지금 다시 재기」만 실제 모델 |
| 로컬 | `python3 -m web --workspace ~/nabi` | 설치된 PC의 화면 — 작업공간 하나 · `inbox/` 스캔 · 실제 파일 이동 |

두 모드가 같은 화면(`site/try/index.html` — 사이드바 있는 앱 대시보드, 화면 구조는 `site/README.md`)을 쓴다.
심사 스크린샷이 「데모 페이지」가 아니라 **제품 화면**이어야 실현가능성 점수가 산다는 판단(2026-09-11 지우 결정)이다.
의존성은 0 — 폐PC·N100에 파이썬만 있으면 뜬다.

```
site/          정적. 서버가 그대로 서빙하고, 배포 때는 Vercel 에 폴더째 올린다
web/server.py  HTTP — 정적 + /api/*  (/try/api/* 도 같은 곳으로)
web/demo.py    방문자 샌드박스 — samples/inbox 복사, 큐는 recorded 행으로 seed, 2시간 만료
web/record.py  recorded.json 생성기 — 엔진을 22건에 돌린다. 숫자는 이 스크립트로만 생긴다
```

## 왜 미리 잰 결과인가

N100은 서류 1건에 약 80초다. 심사위원이 서류를 고를 때마다 모델을 돌리면 5건에 7분을 기다린다.
그래서 **같은 기기에서 미리 잰** `site/try/recorded.json`으로 큐를 즉시 채우고, 어느 기기·언제·어떤 모델인지를
화면 머리와 발에 적는다. 「지금 이 기기에서 다시 재기」를 누르면 그 자리에서 진짜로 돌고 진행이 스트리밍된다(SSE).
한 번에 한 건만 돌고 나머지는 줄을 선다(CPU 하나).

```bash
# N100 에 올린 뒤 (모델 서버가 떠 있어야 한다: python3 -m engine serve)
python3 -m web.record --device "Intel N100 8GB (1호기) · 3스레드" --threads 3
python3 -m web --demo --host 0.0.0.0 --port 8098
```

⚠ 지금 커밋된 `recorded.json`은 **이 노트북(i7-10750H, 4스레드)** 값이다. N100이 오면 위 명령으로 다시 만든다.
프롬프트를 고치면 기록도 다시 만든다(`tests/test_drift.py`가 프롬프트 두 벌의 일치를 지키듯, 기록은 그 프롬프트의 값이다).

## API

`/api/...`와 `/try/api/...` 둘 다 받는다. 체험은 쿠키 `nabi_session`으로 샌드박스를 찾는다.

| | 무엇 |
|---|---|
| `GET /api/health` | 모드 · 기기 · 모델 생존(15초 캐시) · 대기열 · 세션 수 |
| `GET /api/state` | 큐 전체(`docs`) + 정리된 트리(`tree`) + 등록 거래처 + 어휘. 체험이면 세션을 만들고 쿠키를 준다 |
| `GET /api/docs/{id}` | 한 건 — 엔진 행 그대로 + `sample`(정답·형식) + `text_head` + `live` |
| `POST /api/docs/{id}/approve` | `{client?, doc_type?, period?}` — 비우면 그대로 승인, 주면 고쳐서 승인(별칭 학습) → 이동 |
| `POST /api/docs/{id}/reject` | `{why?}` |
| `POST /api/docs/{id}/relabel` | 202 `{job}` — 승인·이동된 건은 409 |
| `GET /api/jobs/{id}/events` | SSE `event: state` — `queued(position)` → `running(elapsed_s)` → `done(row)` / `failed(error)` |
| `POST /api/undo` | 마지막 이동 되돌리기 |
| `POST /api/reset` | (체험) 새 샌드박스 |
| `POST /api/scan` | (로컬) inbox의 새 파일을 큐에 — 같은 내용(sha256)은 건너뛴다 |

행(`docs[]`)은 엔진 `scan_one`이 낸 큐 레코드 그대로다(`pred` 모델 원값 · `client/category/doc_type/period` 규칙 판정 ·
`reasons/explain` 보류 사유 · `target` · `label_s/pp_tps/tg_tps`). 여기에 화면용 세 가지만 얹는다 —
`sample.want`(체험 샘플의 정답, 화면에서 「샘플 정답」 열), `text_head`(모델이 읽은 본문 800자), `live`(이 세션에서 다시 잰 값인가).

⚠ `state.moves`는 이동 대장의 「되돌리지 않은 행」 수인데, 되돌리기는 원래 행을 지우지 않고 `undone` 행을 덧붙이므로 되돌린 뒤에도 줄지 않는다.
화면은 이 값을 쓰지 않고 `docs` 중 `approved` 건수로 「이동 완료」·되돌리기 버튼을 판단한다(2026-09-11). 서버 쪽 정리는 미완.

## 테스트

```bash
python3 -m pytest tests/test_web.py -q
```

진짜 HTTP로 두드린다. 모델은 부르지 않고 `Config.labeler`를 주입해 모델 출력만 넣는다(파서·규칙은 진짜).
세션 격리 · 승인/고쳐서 승인/거절/되돌리기 · 다시 재기 스트리밍 · 모델 실패 · 정적 서빙과 경로 탈출 · 로컬 모드 스캔.

## 아직 없는 것

- 로컬 모드의 **브라우저 업로드** — 지금은 `inbox/` 폴더에 넣고 「스캔」. 설치기 v0와 함께.
- 인증 없음. 체험은 샘플만 만지므로 괜찮지만, 로컬 모드를 `0.0.0.0`에 열지 말 것(127.0.0.1 기본).
- 세션 만료 뒤 화면은 다음 요청에서 새 샌드박스를 받는다(승인 이력이 사라진다) — 2시간 안엔 안 일어난다.
