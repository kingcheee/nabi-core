#!/usr/bin/env bash
# bench.sh — 기기 공통 sLM 실측 (Linux · Termux/Android 공용)
#
#   ① llama-bench (-p 2048 -n 64)  모델 × 스레드 설정별 pp/tg
#   ② 라벨링 실측: samples/text 서류 5건 → {"client","period","doc_type"} JSON (건당 벽시계 초, 프롬프트 토큰, 정답 대조)
#      문서 1건마다 KV 캐시를 새로 연다(cache_prompt=false). 모델은 llama-server에 상주(웜). --cold 면 건마다 llama-cli 프로세스.
#   ③ --thermal: 10분 동안 라벨링을 연속 반복하며 10초마다 온도(termux-battery-status / thermal_zone0) 기록
#
# 사용:
#   bench/bench.sh                          # 기본: 두 모델 × 스레드(nproc, 4) · 서류 5건
#   bench/bench.sh --threads "8,4" --taskset "4-7:4"   # "코어목록:스레드수" — 그 스레드 수일 때만 taskset 적용
#   bench/bench.sh --thermal [--no-sleep]   # ③ 발열 10분을 **덧붙인다**(①②를 건너뛰지 않는다).
#                                           #   ③은 첫 모델·첫 스레드 설정으로 돈다. --models/--threads 를 하나씩 주면 짧다.
#                                           #   ⚠ ①②를 먼저 돌아 기기가 이미 달궈진 상태에서 ③이 시작된다 —
#                                           #     「지속 사용」 곡선으로는 맞고, 「냉간 시작」 곡선이 필요하면 따로 재라.
#   bench/bench.sh --quick                  # 스모크: 모델 1 · 스레드 1설정 · 서류 2건 · bench 1회
#   bench/bench.sh --models qwen            # minicpm5 | qwen | minicpm5,qwen
#   bench/bench.sh --cold                   # 건마다 llama-cli 프로세스(모델 로드 포함)
#
# 결과: bench/results/<host>-<YYYYMMDD-HHMM>.md + .jsonl
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TEXT_DIR="${TEXT_DIR:-$ROOT/samples/text}"
LABELS="${LABELS:-$ROOT/samples/labels.json}"
RESULTS="$HERE/results"; mkdir -p "$RESULTS"
PORT="${PORT:-8097}"
CTX="${CTX:-2048}"
UB="${UB:-128}"
SLEEP_BETWEEN=3
MODELS="minicpm5,qwen"
THREADS=""
TASKSET=""
THERMAL=0; NO_SLEEP=0; QUICK=0; COLD=0; REPS="${REPS:-}"
DOCS="${DOCS:-d01,d03,d07,d13,d19}"   # 텍스트 서류 5건(스캔 제외, 길이 다양)
THERMAL_SECS="${THERMAL_SECS:-600}"

while [ $# -gt 0 ]; do
  case "$1" in
    --models) MODELS="$2"; shift 2;;
    --threads) THREADS="$2"; shift 2;;
    --taskset) TASKSET="$2"; shift 2;;
    --thermal) THERMAL=1; shift;;
    --no-sleep) NO_SLEEP=1; shift;;
    --quick) QUICK=1; shift;;
    --cold) COLD=1; shift;;
    --docs) DOCS="$2"; shift 2;;
    --reps) REPS="$2"; shift 2;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) echo "unknown arg $1"; exit 2;;
  esac
done

# ---------- 플랫폼
IS_TERMUX=0
if [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux ]; then IS_TERMUX=1; fi
if [ $IS_TERMUX -eq 1 ]; then
  export OMP_NUM_THREADS=1
  for b in llama-bench llama-server curl python3; do command -v "$b" >/dev/null 2>&1 || { echo "설치: $b"; pkg install -y llama-cpp curl python; break; }; done
  MODEL_DIRS=("$HOME/models" "$HOME/llm-lab/models")
else
  MODEL_DIRS=("$HOME/models" "$ROOT/models")
fi
for b in llama-bench llama-server python3; do command -v "$b" >/dev/null 2>&1 || { echo "없음: $b (Linux: pacman -S llama-cpp / apt: llama.cpp 빌드)"; exit 1; }; done
if [ $COLD -eq 1 ]; then command -v llama-cli >/dev/null 2>&1 || { echo "없음: llama-cli"; exit 1; }; fi

NPROC=$(nproc 2>/dev/null || echo 4)
[ -z "$THREADS" ] && THREADS="$NPROC,4"
[ $QUICK -eq 1 ] && { MODELS="${MODELS%%,*}"; THREADS="${THREADS%%,*}"; DOCS="d01,d07"; REPS="${REPS:-1}"; }
[ -z "$REPS" ] && { if [ $IS_TERMUX -eq 1 ]; then REPS=1; else REPS=2; fi; }

# ---------- 모델
model_file() {  # tag -> path (없으면 받는다)
  local tag="$1" fname url
  case "$tag" in
    minicpm5) fname="MiniCPM5-2B-Q4_K_M.gguf"; url="https://huggingface.co/openbmb/MiniCPM5-2B-GGUF/resolve/main/$fname";;
    qwen) fname="qwen2.5-1.5b-instruct-q4_k_m.gguf"; url="https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/$fname";;
    *) echo "unknown model tag $tag" >&2; return 1;;
  esac
  local d
  for d in "${MODEL_DIRS[@]}"; do [ -s "$d/$fname" ] && { echo "$d/$fname"; return 0; }; done
  d="${MODEL_DIRS[0]}"; mkdir -p "$d"
  echo "다운로드: $fname → $d" >&2
  curl -L --retry 3 -o "$d/$fname.part" "$url" && mv "$d/$fname.part" "$d/$fname" && echo "$d/$fname"
}

# ---------- 기기 정보
HOST=$(hostname 2>/dev/null || getprop ro.product.model 2>/dev/null || echo device)
HOST=$(echo "$HOST" | tr ' /' '__')
[ $IS_TERMUX -eq 1 ] && HOST="$(getprop ro.product.model 2>/dev/null | tr ' ' '_')"
STAMP=$(date +%Y%m%d-%H%M)
OUT_MD="$RESULTS/$HOST-$STAMP.md"; OUT_JL="$RESULTS/$HOST-$STAMP.jsonl"
CPU=$(grep -m1 -E 'model name|Hardware' /proc/cpuinfo | cut -d: -f2- | sed 's/^ //')
[ -z "$CPU" ] && CPU=$(uname -m)
FEAT=$(grep -m1 -E '^(Features|flags)' /proc/cpuinfo | cut -d: -f2-)
DOTPROD="no"; echo "$FEAT" | grep -qw asimddp && DOTPROD="yes"
AVX2="no"; echo "$FEAT" | grep -qw avx2 && AVX2="yes"
MEM=$(free -m 2>/dev/null | awk '/^Mem:/{print $2" MB"}')
CORES=$(grep -c ^processor /proc/cpuinfo)
PARTS=$(grep -E '^CPU part' /proc/cpuinfo | sort | uniq -c | awk '{printf "%s×%s ", $1, $4}')

log() { echo "$*" | tee -a "$OUT_MD"; }
jl() { echo "$*" >> "$OUT_JL"; }

log "# 실측 — $HOST · $(date '+%Y-%m-%d %H:%M')"
log ""
log "- 기기: $HOST · CPU: $CPU · 코어 $CORES ($PARTS) · RAM $MEM"
log "- asimddp(ARM dotprod): $DOTPROD · avx2: $AVX2 · Termux: $IS_TERMUX · llama.cpp: $(llama-server --version 2>&1 | head -1)"
log "- 설정: ctx $CTX · ubatch $UB · KV f16 · 모델 $MODELS · 스레드 $THREADS · taskset '${TASKSET:-없음}' · 서류 $DOCS · 문서 사이 sleep ${SLEEP_BETWEEN}s · cold=$COLD"
log ""

# ---------- 온도
temp_read() {
  local t=""
  if command -v termux-battery-status >/dev/null 2>&1; then
    t=$(timeout 8 termux-battery-status 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("temperature",""))' 2>/dev/null)
  fi
  local z=""
  for f in /sys/class/thermal/thermal_zone0/temp /sys/class/thermal/thermal_zone1/temp /sys/class/hwmon/hwmon*/temp1_input; do
    [ -r "$f" ] && { z=$(cat "$f" 2>/dev/null); [ -n "$z" ] && [ "$z" -gt 1000 ] 2>/dev/null && z=$((z/1000)); break; }
  done
  echo "${t:-NA},${z:-NA}"
}
# ⚠ /proc/cpuinfo 의 MHz 는 **ARM 에 없다** — 폰에서 이 함수는 NA 만 돌려줬고 2026-09-10 발열 곡선의
#   「CPU MHz」 열이 전부 NA 로 남았다(스로틀링 여부를 못 판정했다). sysfs 를 먼저 보고, 없으면 cpuinfo 로 떨어진다.
cpu_mhz() {
  python3 - <<'PY' 2>/dev/null && return 0
import glob, os, sys
cur = 0
for d in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
    try:
        cur = max(cur, int(open(d).read()))
    except Exception:
        pass
if not cur:
    sys.exit(1)
print(cur // 1000)
PY
  awk '/MHz/{s+=$4;n++} END{if(n) printf "%d", s/n; else print "NA"}' /proc/cpuinfo
}

# ---------- 전원·클럭·메모리 상태
# ⚠ 이 셋이 폰 실측을 무효로 만드는 진짜 원인이다(2026-09-10 실측). 그래서 결과 파일에 같이 남긴다.
#   Note20 은 충전 안 된 상태에서 X1 코어가 3091MHz 중 1747MHz(56%)로 묶이고 여유 RAM 277MB·스왑 2.1GB 로
#   tg 가 0.13 tok/s 까지 내려갔다. 같은 8스레드에서 충전 중인 A34 는 6.72 였다.
power_state() {
  command -v termux-battery-status >/dev/null 2>&1 || { echo "NA"; return; }
  # ⚠ 중첩 따옴표를 피한다 — f-string 안에 \" 를 쓰면 python 문법 오류로 조용히 NA 가 된다(2026-09-10 실측).
  timeout 8 termux-battery-status 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("NA"); raise SystemExit
print("%s/%s %s%% %sC" % (d.get("plugged","?"), d.get("status","?"), d.get("percentage","?"), d.get("temperature","?")))
' 2>/dev/null || echo "NA"
}

clock_state() {  # "현재최대MHz/하드최대MHz (n%)" — sysfs 기준. /proc/cpuinfo 의 MHz 는 ARM 에 없다.
  python3 - <<'PY' 2>/dev/null || echo "NA"
import glob, os
cur = mx = 0
for d in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq")):
    try:
        c = int(open(os.path.join(d, "scaling_cur_freq")).read())
        m = int(open(os.path.join(d, "cpuinfo_max_freq")).read())
    except Exception:
        continue
    cur = max(cur, c); mx = max(mx, m)
if mx:
    print(f"{cur//1000}/{mx//1000}MHz ({round(100*cur/mx)}%)")
else:
    print("NA")
PY
}

mem_state() {
  free -m 2>/dev/null | awk '/^Mem:/{a=$7; f=$4} /^Swap:/{su=$3} END{printf "여유 %sMB · 가용 %sMB · 스왑사용 %sMB", f, a, su}'
}

# ⚠ 이 줄은 **측정이 끝난 직후**에 찍힌다. 클럭은 이미 내려가는 중일 수 있으니 「실행 중 클럭」으로 읽지 마라 —
#   조건을 나중에 해석하기 위한 맥락 표시다. 실행 중 곡선이 필요하면 ③ 발열 모드(10초마다 샘플링)를 쓴다.
state_line() { echo "전원 $(power_state) · 클럭 $(clock_state) · $(mem_state) · 온도 $(temp_read)"; }

# ---------- taskset 결정
ts_for() {  # threads -> "taskset -c X" or ""
  local th="$1"
  [ -z "$TASKSET" ] && { echo ""; return; }
  local cores="${TASKSET%%:*}" only="${TASKSET#*:}"
  if [ "$TASKSET" = "$cores" ] || [ "$only" = "$th" ]; then command -v taskset >/dev/null 2>&1 && echo "taskset -c $cores"; fi
}

# ---------- 시작 상태 · taskset 검사
# taskset 마스크의 코어 수보다 스레드가 많으면 과다구독이다 — 측정값이 무의미해진다.
# 2026-09-10 실측: A34(Dimensity 1080, 빅코어는 6,7 두 개)에 `--taskset "6,7:4"` 를 줘서 4스레드를
# 2코어에 몰아넣었고 tg 가 0.23 tok/s 로 붕괴했다. 빅코어 수만큼만 스레드를 줘라(`"6,7:2"`).
mask_cores() {  # "6,7" 또는 "4-7" -> 코어 개수
  echo "$1" | tr ',' '\n' | python3 -c 'import sys
n=0
for tok in sys.stdin.read().split():
    tok=tok.strip()
    if not tok: continue
    if "-" in tok:
        a,b=tok.split("-"); n+=int(b)-int(a)+1
    else: n+=1
print(n)' 2>/dev/null || echo 0
}
if [ -n "$TASKSET" ]; then
  TS_CORES="${TASKSET%%:*}"; TS_ONLY="${TASKSET#*:}"
  NC=$(mask_cores "$TS_CORES")
  if [ "$TASKSET" != "$TS_CORES" ] && [ "$NC" -gt 0 ] 2>/dev/null && [ "$TS_ONLY" -gt "$NC" ] 2>/dev/null; then
    log "> ⚠ **과다구독**: taskset 코어 $NC 개(\`$TS_CORES\`)에 스레드 $TS_ONLY 개를 주려 한다. 이 행의 값은 쓰지 마라."
    log ""
  fi
fi
log "- 시작 상태: $(state_line)"
log ""

# ---------- ① llama-bench
log "## ① llama-bench (-p 2048 -n 64, r=$REPS)"; log ""
log "| 모델 | 스레드 | taskset | pp2048 tok/s | tg64 tok/s |"; log "|---|---|---|---|---|"
IFS=',' read -ra MTAGS <<< "$MODELS"; IFS=',' read -ra THS <<< "$THREADS"
declare -A MPATH
for tag in "${MTAGS[@]}"; do MPATH[$tag]=$(model_file "$tag") || exit 1; done
for tag in "${MTAGS[@]}"; do
  for th in "${THS[@]}"; do
    TS=$(ts_for "$th")
    out=$($TS llama-bench -m "${MPATH[$tag]}" -p 2048 -n 64 -t "$th" -ub "$UB" -r "$REPS" -o jsonl 2>/dev/null)
    pp=$(echo "$out" | python3 -c 'import sys,json
for l in sys.stdin:
    try: d=json.loads(l)
    except: continue
    if d.get("n_prompt",0)>0 and d.get("n_gen",0)==0: print(round(d["avg_ts"],2))' | head -1)
    tg=$(echo "$out" | python3 -c 'import sys,json
for l in sys.stdin:
    try: d=json.loads(l)
    except: continue
    if d.get("n_gen",0)>0 and d.get("n_prompt",0)==0: print(round(d["avg_ts"],2))' | head -1)
    ST=$(state_line)
    log "| $tag | $th | ${TS:-—} | ${pp:-NA} | ${tg:-NA} |"
    jl "{\"kind\":\"bench\",\"model\":\"$tag\",\"threads\":$th,\"taskset\":\"${TS}\",\"pp\":${pp:-null},\"tg\":${tg:-null},\"state\":\"$ST\"}"
    sleep 5
  done
done
log ""
log "- ① 끝난 뒤 상태: $(state_line)"
log ""

# ---------- 서버 관리
SERVER_PID=""
start_server() {  # tag threads
  local tag="$1" th="$2" TS; TS=$(ts_for "$th")
  $TS llama-server -m "${MPATH[$tag]}" -c "$CTX" -t "$th" -tb "$th" -ub "$UB" -b 512 --port "$PORT" --host 127.0.0.1 -np 1 --no-webui >"$RESULTS/.server-$tag-$th.log" 2>&1 &
  SERVER_PID=$!
  local i; for i in $(seq 1 120); do curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"ok"' && return 0; sleep 1; done
  echo "서버 기동 실패 ($tag, -t $th) — $RESULTS/.server-$tag-$th.log" >&2; return 1
}
stop_server() { [ -n "$SERVER_PID" ] && { kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; SERVER_PID=""; sleep 2; }; }
trap stop_server EXIT

expect_for() { python3 -c 'import sys,json
did=sys.argv[2]
for d in json.load(open(sys.argv[1],encoding="utf-8")):
    if d["id"]==did: print(json.dumps({k:d[k] for k in ("client","client_short","period","doc_type","category")},ensure_ascii=False)); break' "$LABELS" "$1"; }
# ⚠ category 를 빼면 label_one.py 의 match.category 가 항상 거짓이 되고 ok 도 항상 거짓이 된다(2026-09-10 발견).
#   표에 찍는 정확도 3축(고객사·기간·종류)은 그것과 무관하게 맞다 — 성공 기준(기획서 §5.3)이 그 3축이다.

label_doc() {  # tag threads did -> json line
  local tag="$1" th="$2" did="$3" exp; exp=$(expect_for "$did")
  if [ $COLD -eq 1 ]; then
    local TS; TS=$(ts_for "$th"); local pf="$RESULTS/.prompt-$did.txt" of="$RESULTS/.out-$tag-$did.txt"
    python3 "$HERE/label_one.py" --model "$tag" --text "$TEXT_DIR/$did.txt" --print-prompt > "$pf"
    local t0=$(date +%s.%N)
    $TS llama-cli -m "${MPATH[$tag]}" -f "$pf" -n 96 -c "$CTX" -t "$th" -tb "$th" -ub "$UB" --temp 0 -no-cnv -st --no-warmup --log-disable > "$of" 2>/dev/null
    local wall=$(python3 -c "print(round($(date +%s.%N)-$t0,2))")
    python3 "$HERE/label_one.py" --parse-file "$of" --expect "$exp" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); d['wall_s']=$wall; print(json.dumps(d,ensure_ascii=False))"
  else
    python3 "$HERE/label_one.py" --url "http://127.0.0.1:$PORT" --model "$tag" --text "$TEXT_DIR/$did.txt" --expect "$exp"
  fi
}

# ---------- ② 라벨링
log "## ② 라벨링 실측 (서류 1건 = KV 새로, 문서 사이 ${SLEEP_BETWEEN}s)"; log ""
log "| 모델 | 스레드 | 서류 | 글자 | 프롬프트 tok | 출력 tok | pp tok/s | tg tok/s | 벽시계 s | 고객사 | 기간 | 종류 |"
log "|---|---|---|---|---|---|---|---|---|---|---|---|"
IFS=',' read -ra DIDS <<< "$DOCS"
for tag in "${MTAGS[@]}"; do
  for th in "${THS[@]}"; do
    if [ $COLD -eq 0 ]; then start_server "$tag" "$th" || continue; fi
    ok_c=0; ok_p=0; ok_t=0; ok_g=0; n=0; sum_w=0
    for did in "${DIDS[@]}"; do
      line=$(label_doc "$tag" "$th" "$did")
      jl "$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); d.update(kind='label',model='$tag',threads=$th,doc='$did'); print(json.dumps(d,ensure_ascii=False))")"
      row=$(echo "$line" | python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); m=d.get("match",{}); f=lambda b:"O" if b else "X"
print("| %s | %s | %s | %s | %s | %s | %s | %s | %s |"%(d.get("chars","-"),d.get("prompt_n","-"),d.get("predicted_n","-"),round(d.get("pp_tps") or 0,1) or "-",round(d.get("tg_tps") or 0,1) or "-",d.get("wall_s","-"),f(m.get("client")),f(m.get("period")),f(m.get("doc_type"))))
print(int(bool(m.get("client"))),int(bool(m.get("period"))),int(bool(m.get("doc_type"))),d.get("wall_s",0),int(bool(m.get("category"))))')
      r1=$(echo "$row" | head -1); r2=$(echo "$row" | tail -1)
      log "| $tag | $th | $did $r1"
      set -- $r2; ok_c=$((ok_c+$1)); ok_p=$((ok_p+$2)); ok_t=$((ok_t+$3)); sum_w=$(python3 -c "print(round($sum_w+$4,2))"); ok_g=$((ok_g+${5:-0})); n=$((n+1))
      [ $NO_SLEEP -eq 0 ] && sleep $SLEEP_BETWEEN
    done
    log "| **$tag** | **$th** | **평균/정확도** | | | | | | **$(python3 -c "print(round($sum_w/max($n,1),1))")** | **$ok_c/$n** | **$ok_p/$n** | **$ok_t/$n** |"
    jl "{\"kind\":\"summary\",\"model\":\"$tag\",\"threads\":$th,\"n\":$n,\"client_ok\":$ok_c,\"period_ok\":$ok_p,\"doc_type_ok\":$ok_t,\"category_ok\":$ok_g,\"avg_wall_s\":$(python3 -c "print(round($sum_w/max($n,1),2))")}"
    stop_server
  done
done
log ""

# ---------- ③ 발열
if [ $THERMAL -eq 1 ]; then
  tag="${MTAGS[0]}"; th="${THS[0]}"
  log "## ③ 발열 — $tag · -t $th · ${THERMAL_SECS}s 연속 라벨링 · sleep $([ $NO_SLEEP -eq 1 ] && echo 0 || echo $SLEEP_BETWEEN)s"; log ""
  log "| 경과 s | 온도(배터리 °C) | 온도(zone0 °C) | CPU MHz | 완료 건수 | 최근 건 벽시계 s |"; log "|---|---|---|---|---|---|"
  start_server "$tag" "$th" || exit 1
  T0=$(date +%s); done_n=0; last_w="-"
  TEMPLOG="$RESULTS/.temp-$STAMP.log"; : > "$TEMPLOG"
  ( while true; do echo "$(( $(date +%s) - T0 )),$(temp_read),$(cpu_mhz)" >> "$TEMPLOG"; sleep 10; done ) & TP=$!
  i=0
  while [ $(( $(date +%s) - T0 )) -lt "$THERMAL_SECS" ]; do
    did="${DIDS[$(( i % ${#DIDS[@]} ))]}"; i=$((i+1))
    line=$(label_doc "$tag" "$th" "$did"); done_n=$((done_n+1))
    last_w=$(echo "$line" | python3 -c 'import sys,json; print(json.loads(sys.stdin.read()).get("wall_s","-"))')
    jl "$(echo "$line" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); d.update(kind='thermal_label',model='$tag',threads=$th,doc='$did',elapsed=$(( $(date +%s) - T0 ))); print(json.dumps(d,ensure_ascii=False))")"
    tr=$(tail -1 "$TEMPLOG"); IFS=',' read -r el tb tz mhz <<< "$tr"
    log "| $(( $(date +%s) - T0 )) | ${tb:-NA} | ${tz:-NA} | ${mhz:-NA} | $done_n | $last_w |"
    [ $NO_SLEEP -eq 0 ] && sleep $SLEEP_BETWEEN
  done
  kill $TP 2>/dev/null; stop_server
  log ""; log "온도 원자료: $(basename "$TEMPLOG") (경과s,배터리°C,zone0°C,MHz)"
  while IFS=',' read -r el tb tz mhz; do jl "{\"kind\":\"temp\",\"elapsed\":$el,\"battery_c\":\"$tb\",\"zone0_c\":\"$tz\",\"mhz\":\"$mhz\"}"; done < "$TEMPLOG"
fi

log ""; log "끝: $(date '+%H:%M:%S') · 결과 $OUT_MD"
