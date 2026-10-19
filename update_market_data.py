#!/usr/bin/env python3
"""Fetch market data server-side and write market-data.json.

Safety rules:
- Never invent/fill a market value.
- A failed source produces null + an error message.
- Every dynamic quote carries the source timestamp used for display.
- 52-week distance is calculated from fetched 1-year daily highs.

Yahoo Finance's chart endpoint is used as an unofficial market-data source.
It is intentionally called server-side (GitHub Actions) because Yahoo does not
provide browser CORS headers for direct client-side use.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "market-data.json"
UA = "Mozilla/5.0 (compatible; us-market-dashboard/1.0; +https://github.com/)"
YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

SYMBOLS = {
    "VOO": "VOO",
    "QQQM": "QQQM",
    "SCHD": "SCHD",
    "VIX": "^VIX",
    "USDKRW": "KRW=X",        # 1 USD in KRW
    "JPYKRW": "JPYKRW=X",     # 1 JPY in KRW; display is multiplied by 100
    "EURKRW": "EURKRW=X",     # 1 EUR in KRW
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(ts: Optional[float] = None) -> Optional[str]:
    if ts is None:
        return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def finite(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def fetch_json(url: str, retries: int = 3, timeout: int = 18) -> Dict[str, Any]:
    last: Optional[Exception] = None
    for n in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "application/json,text/plain,*/*",
                    "Cache-Control": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if getattr(resp, "status", 200) != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (n + 1))
    raise RuntimeError(f"request failed: {last}")


def yahoo_chart(symbol: str, range_: str, interval: str, include_prepost: bool) -> Dict[str, Any]:
    qs = urllib.parse.urlencode({
        "range": range_,
        "interval": interval,
        "includePrePost": "true" if include_prepost else "false",
        "events": "div,splits",
    })
    url = YAHOO_BASE + urllib.parse.quote(symbol, safe="^=") + "?" + qs
    payload = fetch_json(url)
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(str(chart["error"]))
    result = chart.get("result") or []
    if not result:
        raise RuntimeError("empty chart result")
    return result[0]


def last_valid_point(result: Dict[str, Any]) -> Tuple[Optional[float], Optional[int]]:
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    for ts, close in reversed(list(zip(timestamps, closes))):
        val = finite(close)
        if val is not None:
            return val, int(ts)
    meta = result.get("meta") or {}
    return finite(meta.get("regularMarketPrice")), int(meta.get("regularMarketTime")) if meta.get("regularMarketTime") else None


def daily_history(result: Dict[str, Any], limit: int = 260) -> Tuple[list, Optional[float], Optional[float]]:
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []

    points = []
    valid_highs = []
    valid_lows = []
    for i, ts in enumerate(timestamps):
        c = finite(closes[i]) if i < len(closes) else None
        h = finite(highs[i]) if i < len(highs) else None
        l = finite(lows[i]) if i < len(lows) else None
        if h is not None:
            valid_highs.append(h)
        if l is not None:
            valid_lows.append(l)
        if c is not None:
            points.append({"t": int(ts), "c": round(c, 8)})
    return points[-limit:], (max(valid_highs) if valid_highs else None), (min(valid_lows) if valid_lows else None)


def build_quote(symbol: str) -> Dict[str, Any]:
    intraday = yahoo_chart(symbol, "5d", "5m", True)
    history = yahoo_chart(symbol, "1y", "1d", False)
    meta = intraday.get("meta") or {}

    price, market_ts = last_valid_point(intraday)
    prev = finite(meta.get("chartPreviousClose"))
    hist_points, high_52w, low_52w = daily_history(history)

    # If Yahoo omitted a daily extreme, use the metadata only as a fallback.
    hist_meta = history.get("meta") or {}
    if high_52w is None:
        high_52w = finite(hist_meta.get("fiftyTwoWeekHigh"))
    if low_52w is None:
        low_52w = finite(hist_meta.get("fiftyTwoWeekLow"))

    change = None
    change_pct = None
    if price is not None and prev not in (None, 0):
        change = price - prev
        change_pct = change / prev * 100.0

    high_distance = None
    if price is not None and high_52w not in (None, 0):
        high_distance = (price / high_52w - 1.0) * 100.0

    return {
        "ok": price is not None,
        "symbol": symbol,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "currency": meta.get("currency"),
        "price": round(price, 8) if price is not None else None,
        "previous_close": round(prev, 8) if prev is not None else None,
        "change": round(change, 8) if change is not None else None,
        "change_pct": round(change_pct, 6) if change_pct is not None else None,
        "high_52w": round(high_52w, 8) if high_52w is not None else None,
        "low_52w": round(low_52w, 8) if low_52w is not None else None,
        "from_52w_high_pct": round(high_distance, 6) if high_distance is not None else None,
        "market_time": iso_utc(market_ts),
        "exchange_timezone": meta.get("exchangeTimezoneName"),
        "history_1y": hist_points,
        "source": "Yahoo Finance chart endpoint",
        "source_class": "unofficial_market_data",
    }


def scaled_quote(q: Dict[str, Any], factor: float, unit_note: str) -> Dict[str, Any]:
    out = dict(q)
    for k in ("price", "previous_close", "change", "high_52w", "low_52w"):
        if out.get(k) is not None:
            out[k] = round(float(out[k]) * factor, 8)
    if out.get("history_1y"):
        out["history_1y"] = [{"t": p["t"], "c": round(float(p["c"]) * factor, 8)} for p in out["history_1y"]]
    out["unit_note"] = unit_note
    return out


def cross_rate(a: Dict[str, Any], b: Dict[str, Any], op: str, symbol_name: str) -> Dict[str, Any]:
    """Create a cross quote from two fetched quotes. Only used as a fallback."""
    def calc(x: Optional[float], y: Optional[float]) -> Optional[float]:
        if x is None or y in (None, 0):
            return None
        return x / y if op == "div" else x * y

    price = calc(finite(a.get("price")), finite(b.get("price")))
    prev = calc(finite(a.get("previous_close")), finite(b.get("previous_close")))
    change = price - prev if price is not None and prev is not None else None
    change_pct = change / prev * 100 if change is not None and prev not in (None, 0) else None
    market_times = [x for x in [a.get("market_time"), b.get("market_time")] if x]
    return {
        "ok": price is not None,
        "symbol": symbol_name,
        "name": symbol_name,
        "currency": "KRW",
        "price": round(price, 8) if price is not None else None,
        "previous_close": round(prev, 8) if prev is not None else None,
        "change": round(change, 8) if change is not None else None,
        "change_pct": round(change_pct, 6) if change_pct is not None else None,
        "high_52w": None,
        "low_52w": None,
        "from_52w_high_pct": None,
        "market_time": min(market_times) if market_times else None,
        "history_1y": [],
        "source": f"Yahoo Finance cross rate ({a.get('symbol')} & {b.get('symbol')})",
        "source_class": "unofficial_market_data",
    }


def fetch_krx_gold_official() -> Dict[str, Any]:
    """Optional KRX daily official API. This is NOT an intraday/real-time feed."""
    key = os.environ.get("KRX_AUTH_KEY", "").strip()
    if not key:
        return {
            "ok": False,
            "price": None,
            "change": None,
            "change_pct": None,
            "market_time": None,
            "source": "KRX OPEN API",
            "source_class": "official_daily",
            "status": "KRX_AUTH_KEY_NOT_SET",
            "message": "KRX 공식 Open API 인증키가 없어 가격을 표시하지 않습니다.",
        }

    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    last_error = None
    for back in range(0, 10):
        day = today - dt.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        bas = day.strftime("%Y%m%d")
        url = "https://data-dbg.krx.co.kr/svc/apis/gen/gold_bydd_trd?" + urllib.parse.urlencode({"basDd": bas})
        try:
            req = urllib.request.Request(url, headers={"AUTH_KEY": key, "User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=18) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            rows = payload.get("OutBlock_1") or payload.get("output") or payload.get("data") or []
            for row in rows:
                name = str(row.get("ISU_NM") or "")
                # Select the 1kg 99.99% product by the official product name.
                # Do not rely on a guessed issue code because a wrong code could select another gold product.
                if "99.99" in name and "1kg" in name.lower():
                    price = finite(str(row.get("TDD_CLSPRC", "")).replace(",", ""))
                    ch = finite(str(row.get("CMPPREVDD_PRC", "")).replace(",", ""))
                    pct = finite(str(row.get("FLUC_RT", "")).replace(",", ""))
                    return {
                        "ok": price is not None,
                        "price": price,
                        "change": ch,
                        "change_pct": pct,
                        "market_time": f"{bas[:4]}-{bas[4:6]}-{bas[6:]}T15:30:00+09:00",
                        "source": "KRX OPEN API · 금시장 일별매매정보",
                        "source_class": "official_daily",
                        "status": "DAILY_CLOSE",
                        "message": "KRX 공식 일별 종가입니다. 장중 실시간 가격이 아닙니다.",
                    }
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
    return {
        "ok": False,
        "price": None,
        "change": None,
        "change_pct": None,
        "market_time": None,
        "source": "KRX OPEN API",
        "source_class": "official_daily",
        "status": "NO_DATA",
        "message": f"KRX 공식 데이터 조회 실패: {last_error or 'no recent row'}",
    }


def safe_fetch(key: str, symbol: str, errors: list) -> Dict[str, Any]:
    try:
        return build_quote(symbol)
    except Exception as exc:  # noqa: BLE001
        errors.append({"key": key, "symbol": symbol, "error": str(exc)})
        return {
            "ok": False,
            "symbol": symbol,
            "price": None,
            "previous_close": None,
            "change": None,
            "change_pct": None,
            "high_52w": None,
            "low_52w": None,
            "from_52w_high_pct": None,
            "market_time": None,
            "history_1y": [],
            "source": "Yahoo Finance chart endpoint",
            "source_class": "unofficial_market_data",
            "error": str(exc),
        }


def main() -> int:
    errors = []
    quotes: Dict[str, Dict[str, Any]] = {}

    for key, symbol in SYMBOLS.items():
        quotes[key] = safe_fetch(key, symbol, errors)

    # Fallbacks for KRW crosses if direct Yahoo symbols are unavailable.
    if not quotes["JPYKRW"].get("ok"):
        usdkrw = quotes["USDKRW"]
        usdjpy = safe_fetch("USDJPY_FALLBACK", "JPY=X", errors)
        quotes["JPYKRW"] = cross_rate(usdkrw, usdjpy, "div", "JPYKRW=X")
    if not quotes["EURKRW"].get("ok"):
        eurusd = safe_fetch("EURUSD_FALLBACK", "EURUSD=X", errors)
        quotes["EURKRW"] = cross_rate(eurusd, quotes["USDKRW"], "mul", "EURKRW=X")

    # Korean convention: quote JPY as won per 100 yen.
    if quotes["JPYKRW"].get("ok"):
        quotes["JPYKRW"] = scaled_quote(quotes["JPYKRW"], 100.0, "KRW per 100 JPY")

    krx_gold = fetch_krx_gold_official()

    ok_count = sum(1 for v in quotes.values() if v.get("ok"))
    health = {
        "ok": ok_count >= 6,
        "quote_ok_count": ok_count,
        "quote_total": len(quotes),
        "errors": errors,
    }

    # Avoid creating a new commit when the market/source data did not change.
    # This also means generated_at is the last time the dashboard data actually changed,
    # not merely the last time the workflow woke up.
    previous = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    same_core = (
        previous.get("quotes") == quotes
        and previous.get("krx_gold") == krx_gold
        and previous.get("health") == health
    )
    generated = previous.get("generated_at") if same_core and previous.get("generated_at") else iso_utc()

    payload = {
        "schema_version": 2,
        "generated_at": generated,
        "refresh_policy": "GitHub Actions scheduled; target cadence 15 minutes. Scheduled runs can be delayed by GitHub.",
        "dynamic_source_note": "Yahoo Finance chart endpoint is an unofficial source and may be delayed or unavailable. The dashboard shows source timestamps and stale warnings instead of fabricating values.",
        "quotes": quotes,
        "krx_gold": krx_gold,
        "health": health,
    }

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT)
    print(f"wrote {OUT} with {ok_count}/{len(quotes)} successful Yahoo quotes; KRX ok={krx_gold.get('ok')}")
    if errors:
        print(json.dumps(errors, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if ok_count >= 4 else 2


if __name__ == "__main__":
    raise SystemExit(main())
