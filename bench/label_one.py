#!/usr/bin/env python3
"""서류 1건 라벨링 — llama-server /completion 호출 · JSON 파싱 · 정답 대조. 의존성 0 (urllib·json·re).

출력 스키마(리서치 tax-office-document-labels §4.2 축약): {"client","category","doc_type","period"}
  period 는 정규화 문자열 하나 — 2026-08 · 2026-Q2 · 2026-1H · 2025 · PERMANENT

    label_one.py --url http://127.0.0.1:8097 --model minicpm5 --text samples/text/d05.txt --expect '<labels.json 항목>'
    label_one.py --parse-file out.txt --expect ...       # llama-cli 출력 파일에서 JSON만 파싱 (콜드 모드)
    label_one.py --print-prompt --model qwen --text ...  # 프롬프트만 출력

stdout: JSON 한 줄 — ok, match{client,category,doc_type,period}, pred, expect, prompt_n, predicted_n, prompt_ms,
        predicted_ms, pp_tps, tg_tps, wall_s, chars, raw(앞 300자).
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

DOC_TYPES = ["사업자등록증", "임대차계약서", "법인등기부등본", "통장내역", "급여대장", "근로계약서", "전자세금계산서",
             "종이세금계산서", "종이영수증", "4대보험취득확인서", "거래명세서", "카드매출매입내역", "잔액증명서",
             "원천세신고서", "부가세신고서"]
CATEGORIES = ["기본서류", "정기증빙", "신고증빙"]

SCHEMA = {
    "type": "object",
    "properties": {
        "client": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "doc_type": {"type": "string", "enum": DOC_TYPES},
        "period": {"type": "string"},
    },
    "required": ["client", "category", "doc_type", "period"],
}

INSTRUCTION = (
    "아래는 세무·회계 사무소에 들어온 서류의 본문입니다. 다음 네 가지를 찾아 JSON 객체 하나만 출력하세요. 설명은 쓰지 마세요.\n"
    "- client: 이 서류의 주인인 고객사 상호. 세무대리인·은행·공급자·임대인이 아니라 신고인·예금주·사업장·공급받는자·임차인 쪽.\n"
    "- doc_type: 다음 중 하나: " + ", ".join(DOC_TYPES) + "\n"
    "- category: 사업자등록증·임대차계약서·법인등기부등본 → 기본서류 / 통장내역·급여대장·근로계약서·전자세금계산서·종이세금계산서·종이영수증·4대보험취득확인서 → 정기증빙 / 거래명세서·카드매출매입내역·잔액증명서·원천세신고서·부가세신고서 → 신고증빙\n"
    "- period: 기본서류는 PERMANENT. 월 단위 서류(정기증빙·원천세신고서)는 YYYY-MM. 거래명세서·카드매출매입내역은 분기 YYYY-Q1~Q4. "
    "부가세신고서는 반기 YYYY-1H 또는 YYYY-2H. 잔액증명서는 발급기준일의 연도 YYYY.\n"
    "출력 예: {\"client\": \"○○상사\", \"category\": \"정기증빙\", \"doc_type\": \"급여대장\", \"period\": \"2026-03\"}\n"
    "client 는 반드시 본문에 그대로 적힌 상호를 옮겨 적으세요. 본문에 없는 이름을 만들지 마세요.\n\n[서류 본문]\n"
)


def build_prompt(text: str, model: str, max_chars: int) -> str:
    body = text.strip()[:max_chars]
    p = "<|im_start|>user\n" + INSTRUCTION + body + "\n<|im_end|>\n<|im_start|>assistant\n"
    if model.startswith("minicpm"):
        p += "<think>\n\n</think>\n\n"  # 사고 모드 끄기 — 빈 사고 태그 선주입
    return p


def norm_client(s: str) -> str:
    return re.sub(r"\(주\)|주식회사|㈜|\s+", "", s or "").strip()


def norm_period(s: str) -> str:
    s = (s or "").strip().upper().replace(".", "-").replace("/", "-").replace("년", "-").replace("월", "")
    if "PERMANENT" in s or "영구" in s:
        return "PERMANENT"
    m = re.match(r"^(\d{4})-?\s*(Q[1-4]|[12]H)", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"^(\d{4})-?\s*(\d{1,2})\b", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})", s)
    return m.group(1) if m else s


def parse_json(raw: str) -> dict:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    m = re.search(r"\{.*?\}", raw, flags=re.S)
    if not m:
        return {}
    for cand in (m.group(0), m.group(0).replace("'", '"')):
        try:
            return json.loads(cand)
        except Exception:
            pass
    return {}


def compare(pred: dict, expect: dict) -> dict:
    ec, es = norm_client(expect.get("client", "")), norm_client(expect.get("client_short", ""))
    pc = norm_client(pred.get("client", ""))
    client_ok = bool(pc) and (pc == ec or (es and pc == es) or (es and es in pc) or (len(pc) >= 3 and pc in ec))
    ep = expect.get("period", "")
    if isinstance(ep, dict):
        ep = ep.get("normalized", "")
    return {
        "client": client_ok,
        "category": (pred.get("category", "") or "").strip() == expect.get("category", ""),
        "doc_type": (pred.get("doc_type", "") or "").strip() == expect.get("doc_type", ""),
        "period": norm_period(pred.get("period", "")) == norm_period(ep),
    }


def call_server(url: str, prompt: str, n_predict: int, use_schema: bool, timeout: int) -> dict:
    body = {"prompt": prompt, "n_predict": n_predict, "temperature": 0, "cache_prompt": False, "stream": False, "stop": ["<|im_end|>"]}
    if use_schema:
        body["json_schema"] = SCHEMA
    req = urllib.request.Request(url.rstrip("/") + "/completion", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8097")
    ap.add_argument("--model", default="minicpm5")
    ap.add_argument("--text")
    ap.add_argument("--expect", default="{}")
    ap.add_argument("--max-chars", type=int, default=3500)
    ap.add_argument("--n-predict", type=int, default=120)
    ap.add_argument("--no-schema", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--parse-file")
    ap.add_argument("--print-prompt", action="store_true")
    a = ap.parse_args()
    expect = json.loads(a.expect)

    if a.print_prompt:
        sys.stdout.write(build_prompt(open(a.text, encoding="utf-8").read(), a.model, a.max_chars)); return 0
    if a.parse_file:
        raw = open(a.parse_file, encoding="utf-8", errors="replace").read()
        pred = parse_json(raw[-2500:]); m = compare(pred, expect)
        print(json.dumps({"ok": all(m.values()), "match": m, "pred": pred, "expect": expect, "raw": raw[-300:]}, ensure_ascii=False)); return 0

    text = open(a.text, encoding="utf-8").read()
    prompt = build_prompt(text, a.model, a.max_chars)
    t0 = time.time(); res, err = None, ""
    try:
        res = call_server(a.url, prompt, a.n_predict, not a.no_schema, a.timeout)
    except urllib.error.HTTPError as e:
        err = f"HTTP {e.code}: {e.read()[:200]!r}"
        if not a.no_schema:
            try:
                t0 = time.time(); res = call_server(a.url, prompt, a.n_predict, False, a.timeout); err += " (retry without schema)"
            except Exception as e2:
                err += f" / {e2}"
    except Exception as e:
        err = str(e)
    wall = time.time() - t0
    if res is None:
        print(json.dumps({"ok": False, "error": err, "wall_s": round(wall, 2), "expect": expect}, ensure_ascii=False)); return 1
    raw = res.get("content", ""); pred = parse_json(raw); m = compare(pred, expect); t = res.get("timings", {})
    print(json.dumps({
        "ok": all(m.values()), "match": m, "pred": pred, "expect": expect,
        "prompt_n": t.get("prompt_n"), "predicted_n": t.get("predicted_n"), "prompt_ms": t.get("prompt_ms"), "predicted_ms": t.get("predicted_ms"),
        "pp_tps": t.get("prompt_per_second"), "tg_tps": t.get("predicted_per_second"),
        "wall_s": round(wall, 2), "chars": len(text), "raw": raw[:300], "note": err,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
