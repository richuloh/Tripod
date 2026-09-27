"""
반자동 주문안 계산기 — 판정에 맞추려면 국내 ETF를 몇 주 사고팔아야 하는지 계산한다.
증권사 API 없이 동작한다. 주문은 본인이 직접 넣는다.

  python3 src/order_plan.py                    → 현재 판정 기준 주문안
  python3 src/order_plan.py --state UP_RISKOFF → 특정 판정으로 바뀌면 어떻게 되는지 미리보기
  python3 src/order_plan.py --markdown         → 이슈/메일용 마크다운
  python3 src/order_plan.py --price 418660=42010 --price 133690=135000
                                               → 시세 조회가 안 될 때 수동 입력

계좌 정보 우선순위:
  1) 환경변수 TRIPOD_PORTFOLIO (JSON 문자열 — GitHub Secrets 용)
  2) --portfolio 경로
  3) portfolio.json (리포지토리 루트, .gitignore 됨)
형식은 portfolio.example.json 참고.
"""
import argparse
import json
import math
import os
import sys
import urllib.request

from engine import build, STATE_LABEL, target_text, ROOT

PRICE_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"


def load_portfolio(path):
    raw = os.environ.get("TRIPOD_PORTFOLIO")
    if raw:
        return json.loads(raw), "env:TRIPOD_PORTFOLIO"
    path = path or os.path.join(ROOT, "portfolio.json")
    if not os.path.exists(path):
        return None, path
    return json.load(open(path, encoding="utf-8")), path


def fetch_price(code):
    req = urllib.request.Request(PRICE_URL.format(code=code),
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)["datas"][0]
    return float(d["closePrice"].replace(",", "")), d.get("stockName", "")


def plan(target, krx, port, prices, fee_bps):
    """목표배분 → 종목별 매매수량. 매도 먼저, 매수는 그 대금으로."""
    cash = float(port.get("cash", 0))
    held = {c: int(q) for c, q in port.get("holdings", {}).items()}
    codes = {k: v["code"] for k, v in krx.items() if not k.startswith("_")}
    names = {v["code"]: v["name"] for k, v in krx.items() if not k.startswith("_")}

    total = cash + sum(held.get(c, 0) * prices[c] for c in codes.values())
    budget = total * (1 - fee_bps / 10000.0)

    rows = []
    for key, code in codes.items():
        w = target.get(key, 0.0)
        px = prices[code]
        tgt_qty = math.floor(budget * w / px) if w > 0 else 0
        cur_qty = held.get(code, 0)
        rows.append({"key": key, "code": code, "name": names[code], "price": px,
                     "weight": w, "held": cur_qty, "target": tgt_qty,
                     "delta": tgt_qty - cur_qty})

    sells = [r for r in rows if r["delta"] < 0]
    buys = [r for r in rows if r["delta"] > 0]
    cash_after = (cash + sum(-r["delta"] * r["price"] for r in sells)
                  - sum(r["delta"] * r["price"] for r in buys))
    return {"total": total, "cash_before": cash, "cash_after": cash_after,
            "rows": rows, "sells": sells, "buys": buys}


def fmt_won(x):
    return f"{x:,.0f}원"


def render_text(state, target, p, asof, labels=None):
    out = [f"[{asof}] {STATE_LABEL[state]}  →  {target_text(target, labels)}",
           f"평가액 {fmt_won(p['total'])} (예수금 {fmt_won(p['cash_before'])})", ""]
    if not p["sells"] and not p["buys"]:
        out.append("주문 없음 — 이미 목표배분과 일치합니다.")
    for r in p["sells"]:
        out.append(f"  매도  {r['code']} {r['name']}  {-r['delta']:,}주"
                   f"  @ {fmt_won(r['price'])}  ≈ {fmt_won(-r['delta'] * r['price'])}")
    for r in p["buys"]:
        out.append(f"  매수  {r['code']} {r['name']}  {r['delta']:,}주"
                   f"  @ {fmt_won(r['price'])}  ≈ {fmt_won(r['delta'] * r['price'])}")
    out.append("")
    out.append(f"주문 후 예상 예수금 {fmt_won(p['cash_after'])}")
    out.append("")
    out.append("종목        목표    보유 →   목표수량")
    for r in p["rows"]:
        out.append(f"  {r['code']}  {r['weight'] * 100:5.0f}%  {r['held']:>6,} → {r['target']:>6,}")
    out.append("")
    out.append("* 매도부터 체결시키고 그 대금으로 매수. 시세는 조회 시점 기준이라 실제 체결가와 다를 수 있다.")
    return "\n".join(out)


def render_md(state, target, p, asof, price_src, labels=None):
    out = ["### 주문안 (국내 ETF)", "",
           f"판정 **{STATE_LABEL[state]}** → 목표 **{target_text(target, labels)}**  ",
           f"평가액 {fmt_won(p['total'])} · 예수금 {fmt_won(p['cash_before'])} · 시세 {price_src}", ""]
    if not p["sells"] and not p["buys"]:
        out.append("주문 없음 — 이미 목표배분과 일치합니다.")
    else:
        out += ["| 구분 | 종목 | 수량 | 단가 | 금액 |", "|---|---|---|---|---|"]
        for r in p["sells"]:
            out.append(f"| **매도** | {r['code']} {r['name']} | {-r['delta']:,}주 "
                       f"| {fmt_won(r['price'])} | {fmt_won(-r['delta'] * r['price'])} |")
        for r in p["buys"]:
            out.append(f"| **매수** | {r['code']} {r['name']} | {r['delta']:,}주 "
                       f"| {fmt_won(r['price'])} | {fmt_won(r['delta'] * r['price'])} |")
        out += ["", f"주문 후 예상 예수금 {fmt_won(p['cash_after'])}"]
    out += ["", "<sub>매도 체결 후 매수. 시세는 계산 시점 기준. 주문 전 종목코드와 수량을 앱에서 확인하세요.</sub>"]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="korea")
    ap.add_argument("--state", choices=list(STATE_LABEL), help="가정할 판정 (미리보기)")
    ap.add_argument("--portfolio", help="portfolio.json 경로")
    ap.add_argument("--price", action="append", default=[], help="CODE=PRICE 수동 시세")
    ap.add_argument("--fee-bps", type=float, default=5.0, help="수수료·호가 여유 (기본 5bp)")
    ap.add_argument("--markdown", action="store_true")
    a = ap.parse_args()

    port, src = load_portfolio(a.portfolio)
    if port is None:
        print(f"계좌 정보 없음: {src} 를 만들거나 TRIPOD_PORTFOLIO 를 설정하세요 "
              f"(portfolio.example.json 참고)", file=sys.stderr)
        sys.exit(2)

    cfg, rows = build(preset=a.preset)
    krx = cfg["params"].get("krx")
    if not krx:
        print(f"프리셋 '{a.preset}' 에 krx 매핑이 없습니다.", file=sys.stderr)
        sys.exit(2)
    live = [r for r in rows if r["state"]]
    cur = live[-1]
    state = a.state or cur["state"]
    target = dict(cfg["params"]["alloc"][state])
    labels = cfg["params"].get("labels")

    manual = {}
    for s in a.price:
        c, v = s.split("=")
        manual[c.strip()] = float(v)
    prices, price_src = {}, "네이버 실시간"
    for k, v in krx.items():
        if k.startswith("_"):
            continue
        code = v["code"]
        if code in manual:
            prices[code] = manual[code]
            price_src = "수동 입력"
            continue
        try:
            prices[code], _ = fetch_price(code)
        except Exception as e:  # noqa
            print(f"시세 조회 실패 {code}: {e}\n--price {code}=<가격> 으로 넘겨 주세요.",
                  file=sys.stderr)
            sys.exit(3)

    p = plan(target, krx, port, prices, a.fee_bps)
    if a.markdown:
        print(render_md(state, target, p, cur["date"], price_src, labels))
    else:
        print(render_text(state, target, p, cur["date"], labels))
        if a.state:
            print(f"(가정: {STATE_LABEL[state]} — 현재 실제 판정은 {STATE_LABEL[cur['state']]})")


if __name__ == "__main__":
    main()
