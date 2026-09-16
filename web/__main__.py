#!/usr/bin/env python3
"""웹 서버 CLI.

    python3 -m web --demo                          # 체험(/try) — 샘플 22건 · 방문자별 샌드박스 · 미리 잰 결과
    python3 -m web --workspace ~/nabi              # 설치된 PC 의 로컬 UI — 내 파일
    python3 -m web --demo --host 0.0.0.0 --port 8098 --device "Intel N100 8GB (1호기)"

기본 주소 http://127.0.0.1:8098/ — `/` 랜딩 · `/try/` 체험 · `/download/` · `/phone/`.
모델 서버(llama-server)는 따로 띄운다: `python3 -m engine serve` 가 명령을 알려준다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.label import DEFAULT_URL, health
from web.server import Config, make_server


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="체험 모드 — 샘플만, 업로드 없음, 방문자별 샌드박스")
    mode.add_argument("--workspace", type=Path, help="로컬 모드 — 이 작업공간 하나를 쓴다")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--model", default="minicpm5")
    ap.add_argument("--url", default=DEFAULT_URL, help="llama-server 주소")
    ap.add_argument("--device", default="", help="화면에 표시할 기기 이름(비우면 기록의 기기)")
    ap.add_argument("--site", type=Path, default=ROOT / "site")
    ap.add_argument("--samples", type=Path, default=ROOT / "samples" / "inbox")
    ap.add_argument("--recorded", type=Path, default=ROOT / "site" / "try" / "recorded.json")
    ap.add_argument("--sessions", type=Path, default=ROOT / "web" / ".sessions")
    ap.add_argument("--ttl", type=int, default=7200, help="샌드박스 유지 시간(초)")
    ap.add_argument("--cors-origin", action="append", default=[], metavar="ORIGIN",
                    help="이 origin 의 정적 사이트가 API 를 부르게 허용한다(반복 가능). 예: https://kingcheee.github.io")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if a.demo:
        if not a.recorded.exists():
            print(f"미리 잰 결과가 없다: {a.recorded}\n먼저  python3 -m web.record --device '...'  를 돌려라.")
            return 2
        cfg = Config(site_dir=a.site, mode="demo", samples_dir=a.samples, recorded_path=a.recorded,
                     sessions_dir=a.sessions, model=a.model, model_url=a.url, device=a.device,
                     session_ttl_s=a.ttl, cors_origins=a.cors_origin)
    else:
        cfg = Config(site_dir=a.site, mode="local", workspace=a.workspace, model=a.model, model_url=a.url,
                     device=a.device, cors_origins=a.cors_origin)

    srv = make_server(cfg, host=a.host, port=a.port, quiet=not a.verbose)
    host, port = srv.server_address[:2]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"{'체험' if a.demo else '로컬'} 모드 — http://{shown}:{port}/  (체험 화면 /try/)")
    print(f"모델 서버 {a.url}: {'응답' if health(a.url, timeout=2) else '없음 — 「다시 재기」는 실패로 끝난다'}")
    if a.cors_origin:
        print(f"CORS 허용 origin: {', '.join(a.cors_origin)}")
    if a.demo:
        print(f"샌드박스: {a.sessions}  (유지 {a.ttl}초)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
