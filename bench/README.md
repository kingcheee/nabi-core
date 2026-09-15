# bench — 기기 공통 sLM 실측

같은 서류 5건, 같은 모델 2개, 같은 스크립트로 기기마다 돌려 한 표에 넣는다. 결과는 `bench/results/<기기>-<날짜시각>.md`(표)와 `.jsonl`(원자료).

- 모델: `minicpm5` = MiniCPM5-2B Q4_K_M (Apache-2.0, 영·중 공식 지원 — 한글 토큰 2~3배 팽창) · `qwen` = Qwen2.5-1.5B-Instruct Q4_K_M (Apache-2.0, 한국어 지원)
- 서류: 기본 5건은 `--docs d04,d05,d07,d10,d19` (임대차계약서 docx · 통장내역 xlsx · 급여대장 xlsx · 근로계약서 docx · 원천세신고서 hwpx — 형식·종류를 섞은 텍스트 추출본). 스캔·카톡 사진 정확도는 `--docs d02,d03,d13`으로 따로 잰다. 정답은 `samples/labels.json`(22건)
- 라벨: `{"client","category","doc_type","period"}` JSON을 `json_schema`로 강제(GBNF). 문서 1건마다 KV 캐시를 새로 연다(`cache_prompt=false`). 모델은 llama-server에 상주(웜 상태). 프로세스까지 새로 띄우는 콜드 측정은 `--cold`
- 발열: `--thermal` 10분 연속, 10초마다 `termux-battery-status` 온도(폰) / `thermal_zone0`(읽히는 기기만) / CPU MHz
- 공통 설정(리서치 2026-09-10 `ewaste-slm-device-limits` 근거): `-c 2048` · `-ub 128` · KV 캐시 f16(CPU에서 q8_0/q4_0 금지) · 문서 사이 3초 sleep · MiniCPM5는 빈 사고 태그 `<think>\n\n</think>\n\n` 주입으로 사고 모드 끔

## 공통 절차

⚠️ **Arch 계열은 `llama-cpp` 만 깔면 「no backends are loaded」로 모델 적재가 실패한다** — 백엔드가 별도 패키지다. `sudo pacman -S llama-cpp ggml-cpu`(2026-09-10 실측). 이 배포판 빌드는 `asserts enabled` 경고가 뜨는 디버그 빌드라 속도가 보수적으로 나온다.


```bash
# 이 폴더(bench/)와 samples/text, samples/labels.json 이 같은 상대 위치에 있으면 된다
bench/bench.sh --quick        # 스모크(모델 1·서류 2건·bench 1회) — 처음 한 번
bench/bench.sh                # 본 실측: 두 모델 × 스레드(nproc, 4)
bench/bench.sh --thermal      # 발열 10분 (첫 모델·첫 스레드 설정)
```

모델 파일이 `~/models/`(Termux는 `~/llm-lab/models/`도 봄)에 없으면 스크립트가 Hugging Face에서 받는다(합계 약 2.7GB — 와이파이에서).

## (a) Galaxy A34 — 이 PC에서 ssh로 (2026-09-10 실측 절차)

A34(SM-A346N, 6GB, Dimensity 1080)에는 Termux + `pkg llama-cpp` + 두 모델(`~/llm-lab/models/`)이 이미 있다. 코어 6·7이 A78 빅코어, 0~5가 A55.

`a34`는 아래 예시에서 쓴 ssh 호스트 별칭이다 — Termux에 `sshd`를 띄우고 `~/.ssh/config`에 자기 폰을 등록한 뒤 그 이름으로 바꾼다.

```bash
# 1) 전송 — scp -r 보다 tar 스트림이 빠르고 권한 문제가 없다
cd <저장소 루트>
ssh a34 'mkdir -p ~/bench-lab'
tar czf - bench/bench.sh bench/label_one.py bench/README.md samples/text samples/labels.json \
  | ssh a34 'cd ~/bench-lab && tar xzf - && chmod +x bench/bench.sh && echo OK'

# 2) 실행 — ⚠ setsid 로 세션에서 떼어내야 한다
ssh a34 'cd $HOME/bench-lab && (setsid nohup bash bench/bench.sh \
  --threads "8,4" --taskset "6,7:4" --docs "d04,d05,d07,d10,d19" \
  > $HOME/bench-lab/a34.log 2>&1 < /dev/null &)'

# 3) 진행 확인 / 회수
ssh a34 'tail -20 $HOME/bench-lab/a34.log'
scp 'a34:~/bench-lab/bench/results/*' bench/results/
```

⚠️ **`nohup … &` 만으로는 ssh 가 끊길 때 같이 죽는다**(2026-09-10 실측 — 첫 시도가 조용히 사라졌다). `setsid` 로 새 세션을 만들고 `< /dev/null` 로 stdin을 끊어야 살아남는다. tmux(`tmux new -d -s bench …`)도 된다.

⚠️ **`pgrep -f bench.sh` 로 생존을 판정하지 마라** — ssh 원격 명령 문자열 자체가 매칭돼 죽은 프로세스를 살아 있다고 오판한다(같은 날 실측). 로그 파일의 존재와 마지막 줄(`끝:`)로 판정한다.

- `--taskset "6,7:4"` = 스레드 4일 때만 코어 6·7(A78 빅코어)에 고정. 30분 넘게 돌릴 땐 `--threads 2 --taskset "6,7:2"`가 더 안정적(리서치 §4(1)).
- `OMP_NUM_THREADS=1`은 스크립트가 Termux에서 자동으로 건다.
- 화면 꺼짐·절전이 프로세스를 죽이니 실측 중엔 충전기 연결 + Termux 알림의 「Acquire wakelock」.

## (b) N100 미니PC (1호기)

Linux라면:

```bash
sudo pacman -S llama-cpp python   # Arch 계열. Ubuntu/Debian은 llama.cpp 릴리스 바이너리(https://github.com/ggml-org/llama.cpp/releases, x86_64 avx2 zip)를 풀어 PATH에
git clone <이 저장소> && cd sllm-machine
bench/bench.sh --threads 4,3      # N100은 4코어. 장시간은 -t 3 (PL1 6~10W 안에서 클럭 유지, 리서치 §4(1))
```

Windows라면: llama.cpp 릴리스 `llama-bXXXX-bin-win-cpu-x64.zip`을 풀고, `bench.sh`는 WSL이나 Git Bash에서 돌린다(`llama-server.exe`가 PATH에 있으면 스크립트가 그대로 찾는다). 싱글채널 RAM이라 tg가 40~45% 낮게 나오는 기계임을 결과 머리에 적어 둘 것.

## (c) Galaxy S20 · S21 — 폰에서 직접

1. F-Droid에서 **Termux**(+ **Termux:API**) 설치 → Termux 열고 `pkg update -y && pkg install -y llama-cpp curl python termux-api git`
2. 저장소 받기: `git clone <이 저장소>` (또는 PC에서 `bench/`·`samples/text`·`samples/labels.json`을 카톡/USB로 옮겨 `~/bench-run/`에 같은 구조로)
3. 실행 (모델은 스크립트가 받음, 와이파이):
   ```bash
   cd sllm-machine && bench/bench.sh --quick
   bench/bench.sh --threads 8,4 --taskset 4-7:4      # S20(SD865)·S21(Exynos 2100)은 코어 4~7이 빅/미들
   ```
4. 결과 보내기: `termux-open bench/results/*.md`(공유 시트) 또는 `cp bench/results/* ~/storage/downloads/`(`termux-setup-storage` 먼저) → 카톡·메일로 PC에.
5. 폰마다 코어 배치가 다르니 `cat /proc/cpuinfo | grep 'CPU part'`로 빅코어 번호를 확인해 `--taskset`을 맞춘다(0xd0b=A76, 0xd0d=A77, 0xd41=A78, 0xd44=X1, 0xd05=A55).

## 결과 읽는 법

- ① 표의 `pp2048`이 라벨링 시간을 정한다(시간의 8~9할이 프롬프트 처리). `tg64`는 챗 체감 속도.
- ② 표의 `프롬프트 tok`을 두 모델 사이에서 비교하라 — 같은 서류인데 MiniCPM5가 2~3배 크면 한글 토크나이저 팽창이 실측된 것.
- ③ 표에서 배터리 온도가 45°C를 넘거나 `CPU MHz`가 시작값의 60% 아래로 떨어지면 스로틀링. `--no-sleep`으로 한 번 더 돌려 3초 sleep의 효과를 대조한다.
