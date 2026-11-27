"""本地 K 线行情引擎：SQLite 缓存优先 + Yahoo Finance 自动拉取（美股 / 指数 / 期货 / 加密货币）+ Slash 命令解析 + CSV 导入。"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import httpx

from . import db

# 常用别名 → Yahoo Finance 标准代码
SYMBOL_ALIASES: dict[str, tuple[str, str]] = {
    "SPX": ("^GSPC", "S&P 500 指数"),
    "SP500": ("^GSPC", "S&P 500 指数"),
    "NDX": ("^NDX", "Nasdaq 100 指数"),
    "IXIC": ("^IXIC", "Nasdaq 综合指数"),
    "DJI": ("^DJI", "道琼斯指数"),
    "道指": ("^DJI", "道琼斯指数"),
    "道琼斯": ("^DJI", "道琼斯指数"),
    "VIX": ("^VIX", "恐慌指数 VIX"),
    "QQQ": ("QQQ", "Invesco QQQ Trust"),
    "TQQQ": ("TQQQ", "纳指3倍做多 ETF"),
    "SPY": ("SPY", "SPDR S&P 500 ETF"),
    "IWM": ("IWM", "Russell 2000 ETF"),
    "TLT": ("TLT", "20+ Year Treasury ETF"),
    "GLD": ("GLD", "SPDR Gold ETF"),
    "GOLD": ("GC=F", "黄金期货"),
    "黄金": ("GC=F", "黄金期货"),
    "XAU": ("GC=F", "黄金期货"),
    "XAUUSD": ("GC=F", "黄金期货"),
    "SILVER": ("SI=F", "白银期货"),
    "白银": ("SI=F", "白银期货"),
    "XAG": ("SI=F", "白银期货"),
    "WTI": ("CL=F", "WTI 原油期货"),
    "原油": ("CL=F", "WTI 原油期货"),
    "OIL": ("CL=F", "WTI 原油期货"),
    "SOX": ("^SOX", "费城半导体指数"),
    "SOXX": ("SOXX", "iShares 半导体 ETF"),
    "SMH": ("SMH", "VanEck 半导体 ETF"),
    "XLV": ("XLV", "美股医疗防御 ETF"),
    "EWY": ("EWY", "韩国市场 ETF"),
    "KORU": ("KORU", "韩国3倍做多 ETF"),
    "SCHD": ("SCHD", "美股红利防守 ETF"),
    "DRAM": ("DRAM", "Roundhill Memory ETF"),
    "EUV": ("EUV", "Corgi 光刻与半导体 ETF"),
    "CBRS": ("CBRS", "Cerebras Systems"),
    "ANT": ("ANT", "Anthropic"),
    "ANTHROPIC": ("ANT", "Anthropic"),
    "MU": ("MU", "美光科技 Micron"),
    "美光": ("MU", "美光科技 Micron"),
    "SNDK": ("SNDK", "闪迪 Sandisk"),
    "闪迪": ("SNDK", "闪迪 Sandisk"),
    "WDC": ("WDC", "西部数据 Western Digital"),
    "HYNIX": ("000660.KS", "SK 海力士"),
    "海力士": ("000660.KS", "SK 海力士"),
    "000660.KS": ("000660.KS", "SK 海力士"),
    "NBIS": ("NBIS", "Nebius Group"),
    "LITE": ("LITE", "Lumentum 光模块"),
    "COHR": ("COHR", "Coherent 光模块"),
    "GLW": ("GLW", "康宁 Corning"),
    "MRVL": ("MRVL", "迈威尔 Marvell"),
    "AMD": ("AMD", "AMD 超威"),
    "INTC": ("INTC", "英特尔 Intel"),
    "TSM": ("TSM", "台积电 TSMC"),
    "ASML": ("ASML", "阿斯麦 ASML"),
    "AMAT": ("AMAT", "应用材料 Applied Materials"),
    "LRCX": ("LRCX", "拉姆研究 Lam Research"),
    "QCOM": ("QCOM", "高通 Qualcomm"),
    "RKLB": ("RKLB", "Rocket Lab 商业航天"),
    "HOOD": ("HOOD", "Robinhood"),
    "VST": ("VST", "Vistra AI 核电"),
    "PDD": ("PDD", "拼多多 PDD"),
    "PLTR": ("PLTR", "Palantir"),
    "KO": ("KO", "可口可乐 Coca-Cola"),
    "标普": ("^GSPC", "S&P 500 指数"),
    "纳指": ("^NDX", "Nasdaq 100 指数"),
    "英伟达": ("NVDA", "NVIDIA"),
    "比特币": ("BTC-USD", "Bitcoin / USD"),
    "以太坊": ("ETH-USD", "Ethereum / USD"),
    "恐慌指数": ("^VIX", "恐慌指数 VIX"),
    "ES": ("ES=F", "标普500 迷你期货"),
    "NQ": ("NQ=F", "纳指100 迷你期货"),
    "YM": ("YM=F", "道指 迷你期货"),
    "BTC": ("BTC-USD", "Bitcoin / USD"),
    "BTCUSD": ("BTC-USD", "Bitcoin / USD"),
    "BTCUSDT": ("BTC-USD", "Bitcoin / USD"),
    "ETH": ("ETH-USD", "Ethereum / USD"),
    "ETHUSD": ("ETH-USD", "Ethereum / USD"),
    "ETHUSDT": ("ETH-USD", "Ethereum / USD"),
    "SOL": ("SOL-USD", "Solana / USD"),
    "SOLUSD": ("SOL-USD", "Solana / USD"),
    "SOLUSDT": ("SOL-USD", "Solana / USD"),
    "NVDA": ("NVDA", "NVIDIA"),
    "TSLA": ("TSLA", "Tesla"),
    "AAPL": ("AAPL", "Apple"),
    "MSFT": ("MSFT", "Microsoft"),
    "META": ("META", "Meta"),
    "AMZN": ("AMZN", "Amazon"),
    "GOOG": ("GOOG", "Alphabet"),
    "GOOGL": ("GOOGL", "Alphabet"),
    "COIN": ("COIN", "Coinbase"),
    "MSTR": ("MSTR", "MicroStrategy"),
}

VALID_INTERVALS = ("5m", "15m", "30m", "1h", "4h", "1d", "1wk")
INTRADAY_INTERVALS = ("5m", "15m", "30m", "1h", "4h")

# 美股三大现金指数在日内（4h/15m 等）仅报价 6.5 小时，自动切换为 23 小时连续交易的 CME 期货合约
INTRADAY_FUTURES_MAP: dict[str, str] = {
    "SPX": "ES=F",
    "NDX": "NQ=F",
    "DJI": "YM=F",
}

INTERVAL_STEP_SECONDS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}

# 各周期默认拉取区间与缓存有效期（秒）
INTERVAL_CFG: dict[str, tuple[str, str, int]] = {
    # interval: (yahoo_interval, default_range, ttl_seconds)
    "5m": ("5m", "5d", 180),
    "15m": ("15m", "1mo", 300),
    "30m": ("30m", "1mo", 600),
    "1h": ("60m", "3mo", 900),
    "4h": ("60m", "6mo", 1800),  # 由 1h 自动聚合为 4h
    "1d": ("1d", "2y", 3600),    # 2 年日线确保 EMA200 充分预热
    "1wk": ("1wk", "10y", 7200), # 10 年周线确保 EMA200 充分预热
}


class MarketError(RuntimeError):
    """给用户看的清晰中文行情错误。"""


CN_CANONICAL: dict[str, str] = {
    "黄金": "GOLD",
    "白银": "SILVER",
    "原油": "WTI",
    "标普": "SPX",
    "道指": "DJI",
    "道琼斯": "DJI",
    "纳指": "NDX",
    "英伟达": "NVDA",
    "美光": "MU",
    "闪迪": "SNDK",
    "海力士": "HYNIX",
    "比特币": "BTC",
    "以太坊": "ETH",
    "恐慌指数": "VIX",
    "ANTHROPIC": "ANT",
}


def normalize_symbol(raw: str) -> tuple[str, str, str]:
    """返回 (展示代号, Yahoo 代码, 中文/简称说明)。"""
    cleaned = re.sub(r"^[/$\s]+", "", (raw or "").strip()).upper()
    cleaned = CN_CANONICAL.get(cleaned, cleaned)
    if not cleaned:
        cleaned = "SPX"
    if cleaned in SYMBOL_ALIASES:
        y_sym, label = SYMBOL_ALIASES[cleaned]
        return cleaned, y_sym, label
    return cleaned, cleaned, cleaned


def parse_slash_command(raw: str) -> dict[str, Any]:
    """解析类似 '/SPX'、'/QQQ 15m'、'/BTC 4h'、'/NVDA 1d'、'/tv' 的斜杠命令。"""
    text = (raw or "").strip()
    if not text:
        return {"action": "chart", "symbol": "SPX", "interval": "15m", "force": False}
    parts = [p for p in re.split(r"\s+", text) if p]
    head = parts[0].lstrip("/$").upper()
    if head in ("TV", "SCREENSHOT", "IMG"):
        return {"action": "tv", "raw": text}

    sym, yahoo_sym, label = normalize_symbol(head)
    interval = "15m"
    force = False
    for tok in parts[1:]:
        low = tok.lower()
        if low in VALID_INTERVALS:
            interval = low
        elif low in ("1m",):
            interval = "5m"
        elif low in ("d", "day", "daily"):
            interval = "1d"
        elif low in ("w", "wk", "week", "weekly"):
            interval = "1wk"
        elif low in ("h", "60m"):
            interval = "1h"
        elif low in ("refresh", "reload", "--force", "-f"):
            force = True
    return {
        "action": "chart",
        "symbol": sym,
        "yahooSymbol": yahoo_sym,
        "label": label,
        "interval": interval,
        "force": force,
    }


def _aggregate_4h(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, dict[str, Any]] = {}
    for b in bars:
        bt = (int(b["time"]) // 14400) * 14400
        if bt not in buckets:
            buckets[bt] = {
                "time": bt,
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
            }
        else:
            cur = buckets[bt]
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] += b["volume"]
    return [buckets[k] for k in sorted(buckets.keys())]


def _normalize_bars(bars: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    """按周期对齐时间戳并去重，防止盘中实时跳动时间戳在 SQLite 里堆积成重复条形码蜡烛。"""
    if not bars:
        return []
    ordered = sorted(bars, key=lambda b: int(b["time"]))
    if interval == "4h":
        return _aggregate_4h(ordered)

    buckets: dict[int, dict[str, Any]] = {}
    if interval == "1d":
        for b in ordered:
            bt = (int(b["time"]) // 86400) * 86400
            buckets[bt] = {
                "time": bt,
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b.get("volume", 0.0),
            }
        return [buckets[k] for k in sorted(buckets.keys())]

    if interval == "1wk":
        for b in ordered:
            dt = datetime.fromtimestamp(int(b["time"]), tz=timezone.utc)
            monday = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
            monday_ts = int(monday.timestamp()) - dt.weekday() * 86400
            buckets[monday_ts] = {
                "time": monday_ts,
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b.get("volume", 0.0),
            }
        return [buckets[k] for k in sorted(buckets.keys())]

    step = INTERVAL_STEP_SECONDS.get(interval)
    if not step:
        return ordered
    for b in ordered:
        bt = (int(b["time"]) // step) * step
        if bt not in buckets:
            buckets[bt] = {
                "time": bt,
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b.get("volume", 0.0),
            }
        else:
            cur = buckets[bt]
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] = max(cur["volume"], b.get("volume", 0.0))
    return [buckets[k] for k in sorted(buckets.keys())]


def fetch_yahoo_candles(symbol: str, interval: str = "15m", range_str: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import time

    sym, yahoo_sym, label = normalize_symbol(symbol)
    iv = interval if interval in INTERVAL_CFG else "15m"
    y_iv, default_rg, _ttl = INTERVAL_CFG[iv]
    rg = range_str or default_rg
    is_intraday = iv in INTRADAY_INTERVALS
    fetch_sym = INTRADAY_FUTURES_MAP.get(sym, yahoo_sym) if is_intraday else yahoo_sym

    hosts = (
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
        "query1.finance.yahoo.com",
    )
    last_err: str = ""
    resp: httpx.Response | None = None
    for idx, host in enumerate(hosts):
        url = f"https://{host}/v8/finance/chart/{fetch_sym}"
        try:
            r = httpx.get(
                url,
                params={
                    "interval": y_iv,
                    "range": rg,
                    "includePrePost": "true" if is_intraday else "false",
                },
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Connection": "close"},
                timeout=12.0,
            )
            if r.status_code < 400:
                resp = r
                break
            last_err = f"HTTP {r.status_code}"
            if r.status_code in (400, 404):
                break
        except httpx.HTTPError as exc:
            last_err = str(exc)
        if idx < len(hosts) - 1:
            time.sleep(0.35 * (idx + 1))

    if resp is None:
        raise MarketError(f"拉取 {sym} 行情失败（已自动重试 {len(hosts)} 次）：{last_err}")

    data = resp.json()
    results = (data.get("chart") or {}).get("result") or []
    if not results:
        err = (data.get("chart") or {}).get("error") or {}
        raise MarketError(f"未找到标的 {sym}：{err.get('description') or '无行情数据'}")

    res = results[0]
    meta = res.get("meta") or {}
    timestamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    vols = quote.get("volume") or []

    bars: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        if i >= len(closes) or closes[i] is None or opens[i] is None or highs[i] is None or lows[i] is None:
            continue
        bars.append(
            {
                "time": int(ts),
                "open": round(float(opens[i]), 4),
                "high": round(float(highs[i]), 4),
                "low": round(float(lows[i]), 4),
                "close": round(float(closes[i]), 4),
                "volume": float(vols[i] or 0) if i < len(vols) else 0.0,
            }
        )

    bars = _normalize_bars(bars, iv)

    if not bars:
        raise MarketError(f"{sym} ({iv}) 暂无有效 K 线数据")

    return bars, {
        "symbol": sym,
        "yahoo_sym": fetch_sym,
        "label": meta.get("shortName") or label,
        "currency": meta.get("currency") or "USD",
        "exchange": meta.get("exchangeName") or "",
    }


def save_candles(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
    bars: list[dict[str, Any]],
    *,
    yahoo_sym: str = "",
    currency: str = "USD",
    exchange: str = "",
    replace_overlap: bool = True,
) -> int:
    sym, default_y, _ = normalize_symbol(symbol)
    clean_bars = _normalize_bars(bars, interval)
    with db.DB_LOCK:
        if replace_overlap and clean_bars:
            min_ts = min(int(b["time"]) for b in clean_bars)
            conn.execute(
                "DELETE FROM candles WHERE symbol=? AND interval=? AND ts>=?",
                (sym, interval, min_ts),
            )
        conn.executemany(
            """
            INSERT OR REPLACE INTO candles(symbol, interval, ts, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(sym, interval, int(b["time"]), b["open"], b["high"], b["low"], b["close"], b.get("volume", 0.0)) for b in clean_bars],
        )
        cnt = conn.execute("SELECT COUNT(*) FROM candles WHERE symbol=? AND interval=?", (sym, interval)).fetchone()[0]
        conn.execute(
            """
            INSERT OR REPLACE INTO cache_meta(symbol, interval, yahoo_sym, currency, exchange, bar_count, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sym, interval, yahoo_sym or default_y, currency, exchange, cnt, db.now_iso()),
        )
        conn.commit()
    return int(cnt)


def get_candles(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str = "15m",
    *,
    force: bool = False,
    allow_network: bool = True,
) -> dict[str, Any]:
    """SQLite 缓存优先；未命中或过期时自动拉取并落盘。"""
    sym, yahoo_sym, label = normalize_symbol(symbol)
    iv = interval if interval in INTERVAL_CFG else "15m"
    _y_iv, _rg, ttl = INTERVAL_CFG[iv]

    meta_row = conn.execute("SELECT * FROM cache_meta WHERE symbol=? AND interval=?", (sym, iv)).fetchone()
    stale = True
    if meta_row and not force:
        try:
            fetched = datetime.fromisoformat(meta_row["fetched_at"])
            age = (datetime.now(fetched.tzinfo or timezone.utc) - fetched).total_seconds()
            stale = age > ttl
        except ValueError:
            stale = True

    source = "cache"
    if (stale or not meta_row) and allow_network:
        try:
            bars, info = fetch_yahoo_candles(sym, iv)
            save_candles(
                conn,
                sym,
                iv,
                bars,
                yahoo_sym=info["yahoo_sym"],
                currency=info["currency"],
                exchange=info["exchange"],
            )
            source = "live"
            meta_row = conn.execute("SELECT * FROM cache_meta WHERE symbol=? AND interval=?", (sym, iv)).fetchone()
        except MarketError:
            if not meta_row:
                raise
            source = "cache_fallback"

    rows = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM candles WHERE symbol=? AND interval=? ORDER BY ts ASC",
        (sym, iv),
    ).fetchall()
    if not rows:
        raise MarketError(f"本地库里暂无 {sym} ({iv}) 数据，请检查代码或点击刷新行情")

    candles = [
        {
            "time": int(r["ts"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["volume"]),
        }
        for r in rows
    ]
    last = candles[-1]
    prev = candles[-2] if len(candles) >= 2 else last
    chg = last["close"] - prev["close"]
    chg_pct = (chg / prev["close"] * 100.0) if prev["close"] else 0.0

    return {
        "symbol": sym,
        "yahooSymbol": meta_row["yahoo_sym"] if meta_row else yahoo_sym,
        "label": label,
        "interval": iv,
        "currency": meta_row["currency"] if meta_row else "USD",
        "exchange": meta_row["exchange"] if meta_row else "",
        "source": source,
        "fetchedAt": meta_row["fetched_at"] if meta_row else "",
        "barCount": len(candles),
        "lastPrice": last["close"],
        "change": round(chg, 4),
        "changePct": round(chg_pct, 2),
        "candles": candles,
    }


def prefetch_candles_batch(
    conn: sqlite3.Connection,
    symbols: list[str],
    interval: str = "1d",
    *,
    force: bool = False,
    allow_network: bool = True,
    max_workers: int = 10,
) -> dict[str, dict[str, Any]]:
    """并发拉取多标的 K 线（仅网络请求进线程池，SQLite 读写全部留在主线程），命中本地缓存直接秒返。"""
    iv = interval if interval in INTERVAL_CFG else "1d"
    _y_iv, _rg, ttl = INTERVAL_CFG[iv]

    norm_list: list[str] = []
    for raw in symbols:
        sym, _, _ = normalize_symbol(raw)
        if sym not in norm_list:
            norm_list.append(sym)

    to_fetch: list[str] = []
    if allow_network:
        for sym in norm_list:
            if force:
                to_fetch.append(sym)
                continue
            meta_row = conn.execute("SELECT fetched_at FROM cache_meta WHERE symbol=? AND interval=?", (sym, iv)).fetchone()
            if not meta_row:
                to_fetch.append(sym)
                continue
            try:
                fetched = datetime.fromisoformat(meta_row["fetched_at"])
                age = (datetime.now(fetched.tzinfo or timezone.utc) - fetched).total_seconds()
                if age > ttl:
                    to_fetch.append(sym)
            except ValueError:
                to_fetch.append(sym)

    if to_fetch:
        fetched_results: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=min(max_workers, len(to_fetch))) as pool:
            futs = {pool.submit(fetch_yahoo_candles, sym, iv): sym for sym in to_fetch}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    bars, info = fut.result()
                    fetched_results.append((sym, bars, info))
                except MarketError:
                    continue
        for sym, bars, info in fetched_results:
            save_candles(
                conn,
                sym,
                iv,
                bars,
                yahoo_sym=info["yahoo_sym"],
                currency=info["currency"],
                exchange=info["exchange"],
            )

    out: dict[str, dict[str, Any]] = {}
    for sym in norm_list:
        try:
            out[sym] = get_candles(conn, sym, iv, force=False, allow_network=False)
        except MarketError:
            continue
    return out


def import_csv_ohlcv(conn: sqlite3.Connection, symbol: str, interval: str, csv_text: str) -> int:
    """支持直接导入 TradingView / 券商导出的标准 OHLCV CSV 到本地 SQLite K 线库。"""
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    bars: list[dict[str, Any]] = []
    for row in reader:
        low = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        t_raw = low.get("time") or low.get("timestamp") or low.get("date") or low.get("datetime") or ""
        if not t_raw:
            continue
        ts: int | None = None
        if re.fullmatch(r"\d{10,13}", t_raw):
            val = int(t_raw)
            ts = val // 1000 if val > 10_000_000_000 else val
        else:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(t_raw[:19], fmt).replace(tzinfo=timezone.utc)
                    ts = int(dt.timestamp())
                    break
                except ValueError:
                    continue
        if ts is None:
            continue
        try:
            bars.append(
                {
                    "time": ts,
                    "open": float(low["open"]),
                    "high": float(low["high"]),
                    "low": float(low["low"]),
                    "close": float(low["close"]),
                    "volume": float(low.get("volume") or 0),
                }
            )
        except (KeyError, ValueError):
            continue
    if not bars:
        raise MarketError("CSV 中未解析出有效 OHLCV 行（需包含 time/date, open, high, low, close 列）")
    return save_candles(conn, symbol, interval, bars, exchange="CSV Import")


def list_cache_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT symbol, interval, yahoo_sym, currency, exchange, bar_count, fetched_at FROM cache_meta ORDER BY fetched_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def quick_symbols() -> list[dict[str, str]]:
    featured = ["SPX", "QQQ", "SPY", "NVDA", "TSLA", "AAPL", "BTC", "ETH", "SOL", "GOLD", "TLT", "VIX", "ES", "NQ"]
    return [{"symbol": s, "yahoo": SYMBOL_ALIASES[s][0], "label": SYMBOL_ALIASES[s][1]} for s in featured]


def get_multi_asset_snapshot(
    conn: sqlite3.Connection,
    symbols: list[str],
    *,
    days: int = 14,
    force: bool = False,
) -> dict[str, Any]:
    """一次拉取多资产日线并归一化涨跌幅，专门用于宏观大波动 / 跨市场暴跌与反转快照图。"""
    clean_syms: list[str] = []
    for raw in symbols or ["NDX", "SPX", "NVDA", "BTC", "GOLD"]:
        sym, _, _ = normalize_symbol(raw)
        if sym not in clean_syms:
            clean_syms.append(sym)
    clean_syms = clean_syms[:8]

    palette = ["#2563eb", "#16a34a", "#dc2626", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#4b5563"]
    assets: list[dict[str, Any]] = []
    for idx, sym in enumerate(clean_syms):
        try:
            data = get_candles(conn, sym, "1d", force=force)
        except MarketError:
            continue
        bars = data["candles"][-max(7, min(days, 60)) :]
        if len(bars) < 2:
            continue
        base_close = bars[0]["close"] or 1.0
        # 计算：窗口期最大回撤（从高点到最低点）、最近 5 根前跌势低点、以及最新一根相对前一根的涨跌幅
        peak = max(b["high"] for b in bars)
        trough = min(b["low"] for b in bars)
        peak_to_trough = round(((trough - peak) / peak) * 100.0, 2) if peak else 0.0
        last = bars[-1]
        prev = bars[-2]
        day_chg = round(((last["close"] - prev["close"]) / prev["close"]) * 100.0, 2) if prev["close"] else 0.0
        # 近似上周单周变化（倒数第 6 根到倒数第 2 根，覆盖周一至周五）
        wk_bars = bars[-6:-1] if len(bars) >= 6 else bars[:-1]
        wk_chg = 0.0
        if len(wk_bars) >= 2 and wk_bars[0]["open"]:
            wk_chg = round(((wk_bars[-1]["close"] - wk_bars[0]["open"]) / wk_bars[0]["open"]) * 100.0, 2)

        series = [
            {"time": b["time"], "value": round(((b["close"] - base_close) / base_close) * 100.0, 2), "close": b["close"]}
            for b in bars
        ]
        assets.append(
            {
                "symbol": sym,
                "label": data["label"],
                "color": palette[idx % len(palette)],
                "lastPrice": last["close"],
                "dayChangePct": day_chg,
                "prevWeekPct": wk_chg,
                "maxDrawdownPct": peak_to_trough,
                "lowPrice": trough,
                "highPrice": peak,
                "series": series,
            }
        )
    return {"days": days, "assets": assets}
