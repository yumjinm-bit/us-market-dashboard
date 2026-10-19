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
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
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


def _daily_close_pairs(result: Dict[str, Any]) -> list[tuple[int, float]]:
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    pairs = []
    for i, ts in enumerate(timestamps):
        c = finite(closes[i]) if i < len(closes) else None
        if c is not None:
            pairs.append((int(ts), c))
    return pairs


def _local_date(ts: int, tz_name: Optional[str]) -> dt.date:
    d = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc)
    try:
        return d.astimezone(ZoneInfo(tz_name or "UTC")).date()
    except Exception:
        return d.date()


def previous_completed_close(history: Dict[str, Any], market_ts: Optional[int], tz_name: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """Most recent completed daily close before the current market date.

    Do NOT use Yahoo meta.chartPreviousClose here: with a multi-day range it is
    the close before the requested range, not necessarily yesterday's close.
    """
    pairs = _daily_close_pairs(history)
    if not pairs:
        return None, None
    if market_ts:
        market_date = _local_date(int(market_ts), tz_name)
        prior = [(ts, c) for ts, c in pairs if _local_date(ts, tz_name) < market_date]
        if prior:
            ts, c = prior[-1]
            return c, _local_date(ts, tz_name).isoformat()
    ts, c = pairs[-2] if len(pairs) >= 2 else pairs[-1]
    return c, _local_date(ts, tz_name).isoformat()


def ytd_reference(history: Dict[str, Any], market_ts: Optional[int], tz_name: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """Final valid close before Jan 1 of the current market year."""
    pairs = _daily_close_pairs(history)
    if not pairs:
        return None, None
    ref_ts = int(market_ts or pairs[-1][0])
    year = _local_date(ref_ts, tz_name).year
    cutoff = dt.date(year, 1, 1)
    prior = [(ts, c) for ts, c in pairs if _local_date(ts, tz_name) < cutoff]
    if not prior:
        return None, None
    ts, c = prior[-1]
    return c, _local_date(ts, tz_name).isoformat()


def build_quote(symbol: str) -> Dict[str, Any]:
    intraday = yahoo_chart(symbol, "5d", "5m", True)
    history = yahoo_chart(symbol, "1y", "1d", False)
    meta = intraday.get("meta") or {}

    price, market_ts = last_valid_point(intraday)
    hist_meta = history.get("meta") or {}
    tz_name = meta.get("exchangeTimezoneName") or hist_meta.get("exchangeTimezoneName")
    prev, prev_date = previous_completed_close(history, market_ts, tz_name)
    ytd_base, ytd_base_date = ytd_reference(history, market_ts, tz_name)
    hist_points, high_52w, low_52w = daily_history(history)

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

    ytd_price_return = None
    if price is not None and ytd_base not in (None, 0):
        ytd_price_return = (price / ytd_base - 1.0) * 100.0

    return {
        "ok": price is not None,
        "symbol": symbol,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "currency": meta.get("currency"),
        "price": round(price, 8) if price is not None else None,
        "previous_close": round(prev, 8) if prev is not None else None,
        "previous_close_date": prev_date,
        "previous_close_source": "previous_completed_daily_close",
        "change": round(change, 8) if change is not None else None,
        "change_pct": round(change_pct, 6) if change_pct is not None else None,
        "high_52w": round(high_52w, 8) if high_52w is not None else None,
        "low_52w": round(low_52w, 8) if low_52w is not None else None,
        "from_52w_high_pct": round(high_distance, 6) if high_distance is not None else None,
        "ytd_base_price": round(ytd_base, 8) if ytd_base is not None else None,
        "ytd_base_date": ytd_base_date,
        "ytd_price_return_pct": round(ytd_price_return, 6) if ytd_price_return is not None else None,
        "market_time": iso_utc(market_ts),
        "exchange_timezone": tz_name,
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
        "previous_close_date": None,
        "previous_close_source": "cross_rate_components",
        "change": round(change, 8) if change is not None else None,
        "change_pct": round(change_pct, 6) if change_pct is not None else None,
        "high_52w": None,
        "low_52w": None,
        "from_52w_high_pct": None,
        "ytd_base_price": None,
        "ytd_base_date": None,
        "ytd_price_return_pct": None,
        "market_time": min(market_times) if market_times else None,
        "history_1y": [],
        "source": f"Yahoo Finance cross rate ({a.get('symbol')} & {b.get('symbol')})",
        "source_class": "unofficial_market_data",
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
            "previous_close_date": None,
            "previous_close_source": "previous_completed_daily_close",
            "change": None,
            "change_pct": None,
            "high_52w": None,
            "low_52w": None,
            "from_52w_high_pct": None,
            "ytd_base_price": None,
            "ytd_base_date": None,
            "ytd_price_return_pct": None,
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


    generated = iso_utc()
    ok_count = sum(1 for v in quotes.values() if v.get("ok"))
    payload = {
        "schema_version": 2,
        "generated_at": generated,
        "refresh_policy": "GitHub Actions scheduled; target cadence 15 minutes on weekdays. Runs can be delayed by GitHub.",
        "dynamic_source_note": "Yahoo Finance chart endpoint is an unofficial source and may be delayed or unavailable. Daily change uses the previous completed daily close; YTD uses the previous calendar year's final close. The dashboard shows timestamps and stale warnings instead of fabricating values.",
        "quotes": quotes,
        "health": {
            "ok": ok_count >= 6,
            "quote_ok_count": ok_count,
            "quote_total": len(quotes),
            "errors": errors,
        },
    }

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT)
    print(f"wrote {OUT} with {ok_count}/{len(quotes)} successful Yahoo quotes")
    if errors:
        print(json.dumps(errors, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if ok_count >= 4 else 2


if __name__ == "__main__":
    raise SystemExit(main())
