# 사이트 — 한 도메인 네 경로 (제품 사이트 「나비」)

원티드 제출 링크 하나에 네 경로가 산다. **랜딩 + 앱 대시보드**로, 문서·포트폴리오가 아니라 배포 중인 SaaS 제품처럼 읽히게 만든다(2026-09-11 재설계).

| 경로 | 무엇 | 어디서 도나 | 상태 |
|---|---|---|---|
| `/` | 제품 랜딩 — 히어로·시연 영상·문제 한 줄·작동 방식·기능·AI 활용 방식·실측·지원 기기·요금(가격 없음)·FAQ·도구·라이선스 | **정적 호스팅**(GitHub Pages) | ✅ `index.html` (파일 하나, CSS 인라인, 외부 자원 0) |
| `/try` | **앱 대시보드** — 대시보드 / 받은 서류함 / 확인 큐 / 정리된 서류 / 거래처 / 기기. 설치된 PC의 로컬 화면과 같은 화면 | 화면은 정적, API만 인스턴스 | ✅ `try/index.html` + `try/recorded.json` — 서버는 `web/` |
| `/download` | 설치 — OS 카드 3(준비 중)·요구 사양·설치하면 생기는 것·흐름·개발자 명령·라이선스 | 정적 호스팅 | ✅ 페이지. **zip은 준비 중** |
| `/phone` | 지원 기기 · 벤치마크 — 기기 카드·실측표 12행·설정 하나로 5.3배·발열 SVG 그래프·폐폰 기준선·Termux 절차 | 정적 호스팅 | ✅ 페이지. **영상은 촬영 예정** |

```
site/
  index.html            랜딩 — 파일 하나. CSS는 assets/app.css 와 같은 값을 인라인으로 품는다(값을 바꾸면 양쪽을 맞춘다)
  assets/app.css        디자인 시스템 — /try · /download · /phone 이 <link>로 쓴다
  assets/dashboard.jpg  랜딩 히어로의 제품 스크린샷 (tools/site_shots.js 의 02 컷을 1600 폭 JPG로)
  try/index.html        앱 화면 — 바닐라 JS 한 파일, 해시 라우팅(#/dashboard #/inbox #/queue #/organized #/clients #/device)
  try/recorded.json     미리 잰 22건 — python3 -m web.record 로만 만든다
  download/ phone/      제품 페이지
```

## 디자인 시스템 (`assets/app.css`)

- 워드마크 **「나비」**(잠정명 — 확정되면 `grep -rl 나비 site/`로 바꾼다). 로고 마크는 인라인 SVG(초록 사각 안 두 삼각형 날개). 이미지·아이콘 폰트 없음.
- 흰 배경 `#fff` · 서피스 `#f8fafc` · 경계 `#e5e7eb` · 본문 `#0f172a` · 보조 `#64748b`. 강조 초록 `#15803d`(연한 `#dcfce7`), 보류 앰버 `#b45309/#fef3c7`, 거절 빨강 `#b91c1c/#fee2e2`, 이동완료 파랑 `#1d4ed8/#dbeafe`. **라이트 전용**.
- 글꼴은 시스템 스택(Pretendard → Apple SD Gothic Neo → Malgun Gothic → Noto Sans KR). 웹폰트 없음.
- 아이콘은 각 페이지 `<body>` 첫머리의 `<svg hidden>` 스프라이트(`<symbol id="i-…">`, 24px, stroke 1.75)를 `<use href="#i-…">`로 쓴다.
- 컴포넌트: `.btn-primary/secondary/ghost` · `.badge-ready/held/approved/rejected/live/soon/gray` · `.card` · `.stat` · `.table-wrap table.data` · 사이드바 240px · 상단바 56px · 우측 드로어 420px(본문을 덮지 않고 민다) · 토스트 3초.
- 거북이 디자인(크림 배경·형광펜·픽셀 소제목)은 **이 사이트에 쓰지 않는다** — 그건 개인 문서 규격이다.

## `/try` 화면 구조

데이터는 `GET ./api/state` 하나(+ 실패 시 `./recorded.json` 읽기 전용 폴백과 앰버 배너). API 표는 `web/README.md`.

| 해시 | 화면 | 내용 |
|---|---|---|
| `#/dashboard` | 대시보드 | 타일 4(받은 서류·확정·보류·이동 완료) · 보류 사유 분포(막대) · 최근 활동(승인·거절·다시 잼) · 처리 시간(평균·최소·최대·구간) · 체험 전용 「샘플 정답 대조」(규칙 판정 vs 샘플 정답, 사람이 고친 건 제외) |
| `#/inbox` | 받은 서류함 | 상태 필터 칩 · 표(상태·파일명·형식·거래처·서류 종류·기간·모델 초) · 행 클릭 → **드로어**(4축 표: 모델 원값/규칙 판정/샘플 정답 · 판정·보류 사유 · 목표 경로 · 승인/고쳐서 승인/거절/다시 재기(SSE) · 측정 · 모델이 읽은 본문) |
| `#/queue` | 확인 큐 | 보류 카드(사유 + 인라인 수정 폼) · 「확정 n건 전부 승인」 · 확정 목록(개별 승인) |
| `#/organized` | 정리된 서류 | 접이식 폴더 트리 · 이동한 파일 표(경로·원본·시각) |
| `#/clients` | 거래처 | 등록 거래처별 서류 수(확정·보류·이동) · 별칭 학습 설명 |
| `#/device` | 기기 | 인스턴스(기기·모델·스레드·모드·대기열) · 미리 잰 기록(pp/tg tok/s) · 벤치마크 링크 |

상단바 검색은 클라이언트 필터(파일명·거래처·서류 종류). 「마지막 이동 되돌리기」는 이동 완료 건이 있을 때만 켜진다. 로컬 모드면 「받은 서류함 스캔」이 보인다.

## 로컬에서 보기

```bash
# 모델 서버 (다시 재기가 실제로 돌게 하려면)
llama-server -m ~/models/MiniCPM5-2B-Q4_K_M.gguf --host 127.0.0.1 --port 8097 -c 2048 -ub 128 -t 4 --no-warmup &

python3 -m web --demo            # → http://127.0.0.1:8098/  (앱 /try/)
```

정적만 보려면 `python3 -m http.server 8123 --bind 127.0.0.1 --directory site` — 이때 `/try`는 API가 없으므로
「체험 인스턴스가 응답하지 않습니다」 배너와 함께 `recorded.json`을 읽기 전용으로 보여준다(액션 전부 비활성). **이것이 N100이 죽었을 때의 모습이다.**

⚠ Claude in Chrome은 `file://`을 못 열고, Hyprland 타일링에서는 창을 1600×900으로 못 만들 수 있다. 검수·스크린샷은 아래 Playwright 스크립트가 확실하다.

## 심사용 스크린샷 (원티드 폼 — 대표 이미지 1 + 스크린샷 최대 5, 16:9)

```bash
NODE_PATH=<playwright가 있는 node_modules> node tools/site_shots.js <출력폴더>
```

1600×900으로 다섯 장 — `01-landing` · `02-inbox-drawer`(대표 이미지) · `03-queue`(수정 폼 열림) · `04-organized` · `05-dashboard`(활동 있음).
새 방문자 세션에서 확정 7건 승인 + 보류 1건 고쳐서 승인한 상태를 만든 뒤 찍는다. 히어로 스크린샷은
`magick 02-inbox-drawer.png -resize 1600x -quality 82 -strip site/assets/dashboard.jpg`(300KB 이하).

## 랜딩을 정적으로 분리하는 이유

**심사 기간(9/21~10/5) 링크가 죽으면 실격이다**(참가약관 제6조 3항 — 제3자 평가 솔루션이 링크를 읽는다).
체험 인스턴스는 집 회선의 N100에서 돌기 때문에 죽을 수 있다. 그래서 랜딩은 인스턴스와 **다른 곳에서**
도는 정적 파일로 두고, `/try`도 화면·기록은 정적으로 두어 인스턴스가 멈춰도 링크와 심사 대상 내용은 살아 있게 한다.

랜딩은 `index.html` 파일 **하나**다(히어로 이미지만 `assets/dashboard.jpg` — 없어도 페이지는 산다). 빌드도, 외부 스크립트도, 웹폰트도 없다 —
「외부 API 호출 0」이라고 적어 놓은 페이지가 남의 서버를 부르면 안 된다. 다른 세 페이지도 외부 자원을 쓰지 않는다.
자동 평가기가 읽을 텍스트(문제 정의 · AI 활용 방식 · 사용 도구·라이선스)는 랜딩 본문에 그대로 있다.

## 배포 — GitHub Pages (2026-09-16 현재 이것이 제출 링크)

**공개 주소 `https://kingcheee.github.io/nabi-core/`** — 공개 저장소 `kingcheee/nabi-core`의 `gh-pages` 브랜치(루트 = 이 `site/` 폴더 내용)를 GitHub Pages가 그대로 서빙한다. 빌드 없음. 네 경로 전부 상대 링크라 `/nabi-core/` 하위 경로에서도 산다. `/try/`는 API가 없으므로 미리 잰 22건을 읽기 전용으로 보여준다(배너).

갱신 절차 — 이 repo의 `site/`를 고친 뒤:

```bash
cd ~/projects/03-personal/sllm-machine && python3 -m pytest tests/test_web.py -q      # 사이트 200·<h1>
rsync -a --delete --exclude README.md site/ ../nabi-core/site/                         # 공개 repo main 에 복사
cd ../nabi-core && git add site && git commit -m "site: …" && git push origin main
git worktree add /tmp/nabi-ghp gh-pages && rsync -a --delete --exclude .git site/ /tmp/nabi-ghp/ \
  && git -C /tmp/nabi-ghp add -A && git -C /tmp/nabi-ghp commit -m "pages: …" && git -C /tmp/nabi-ghp push origin gh-pages \
  && git worktree remove /tmp/nabi-ghp
curl -sI https://kingcheee.github.io/nabi-core/ | head -1                                # 1～2분 뒤 반영
```

- 도메인을 사면: gh-pages 루트에 `CNAME` 파일(도메인 한 줄) + DNS `CNAME → kingcheee.github.io.` → 저장소 Settings → Pages에서 Enforce HTTPS. 제출 링크만 바꾸면 되고 사이트는 손댈 것 없다.
- **체험 인스턴스**(2026-09-16부터): `try/index.html`의 `<meta name="nabi-api">`가 인스턴스 주소다. 화면은 같은 origin(`./api`)을 먼저 시도하고, 안 되면(GitHub Pages) 그 주소를 부르고, 둘 다 없으면 `recorded.json` 읽기 전용. 인스턴스 쪽은 `python3 -m web --demo --cors-origin https://kingcheee.github.io`로 그 origin을 허용하고(프리플라이트·`Access-Control-Allow-Origin`), 제3자 쿠키가 막힌 브라우저를 위해 세션은 `X-Nabi-Session` 헤더로 이어진다(화면이 localStorage에 보관). HTTPS 노출은 Tailscale Funnel(`tailscale funnel --bg 8098`).
- 시연 영상은 `assets/nabi-demo.mp4`(11MB, 같은 origin, `preload="none"`)와 포스터 `assets/nabi-demo-poster.jpg`. 유튜브 `https://youtu.be/Y4yoZT6KcuE`는 링크만 — 임베드(외부 스크립트) 안 한다.

## 손대기 전에

- 페이지에 적힌 숫자는 전부 우리가 잰 값이다. **재지 않은 숫자를 넣지 않는다.** 못 잰 칸은 「예정」, 없는 것은 「준비 중」으로 남긴다.
  조사로 잡은 기준선(폐폰 6GB 등)은 「조사 기준」이라고 적는다. 고객·후기·사용자 수·버전·날짜를 지어내지 않는다.
- `try/recorded.json`은 손으로 고치지 않는다 — `python3 -m web.record`로만 만든다(지금은 이 노트북 값, N100이 오면 다시).
- 제품 이름 「나비」는 잠정명이다. 요금 섹션은 넣되 가격은 적지 않는다(지우 결정 2026-09-11).
- 저장소 링크는 공개 저장소 https://github.com/kingcheee/nabi-core 만 건다. 죽은 링크를 걸지 않는다.
- 시연 영상(46초)은 랜딩 `#demo`에 같은 origin `<video>`로 붙어 있다(2026-09-16). 폰 터미널·폐PC(AVX1) 설치 영상은 아직 「촬영 예정」.
- `tests/test_web.py`가 `/`·`/try`의 200과 `<h1>`을 본다 — 랜딩·앱에 `<h1>`은 남긴다.
