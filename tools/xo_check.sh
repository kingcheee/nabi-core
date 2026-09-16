#!/usr/bin/env bash
# 교차 출처 체험 검증 — 정적 사이트(8123)와 인스턴스(8099)를 다른 origin 으로 띄워 Playwright 로 두드린다.
#   NODE_PATH=<playwright 가 있는 node_modules> tools/xo_check.sh
# 검사: 배너 없음 · 세션 localStorage 보관 · 전부 승인 → 새로고침 뒤 유지 · 다른 방문자 격리 · 다시 재기(SSE, 모델 서버 8097 필요)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$(mktemp -d)"; trap 'kill ${API_PID:-} ${ST_PID:-} 2>/dev/null || true; rm -rf "$S"' EXIT
mkdir -p "$S/site" "$S/sessions"; cp -r "$ROOT/site/." "$S/site/"
sed -i 's#<meta name="nabi-api" content="[^"]*">#<meta name="nabi-api" content="http://127.0.0.1:8099/">#' "$S/site/try/index.html"
grep -q 'content="http://127.0.0.1:8099/"' "$S/site/try/index.html"
( cd "$ROOT" && python3 -m web --demo --port 8099 --cors-origin http://127.0.0.1:8123 --sessions "$S/sessions" > "$S/api.log" 2>&1 ) & API_PID=$!
python3 -m http.server 8123 --bind 127.0.0.1 --directory "$S/site" > /dev/null 2>&1 & ST_PID=$!
sleep 2; curl -sf -o /dev/null http://127.0.0.1:8099/api/health; curl -sf -o /dev/null http://127.0.0.1:8123/try/
node "$ROOT/tools/live_check.js" http://127.0.0.1:8123/ --act --expect-api http://127.0.0.1:8099/
