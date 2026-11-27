"""双极量化波动率雷达：同时监控「波动率极端放大（反转大机会）」与「波动率极致收窄（变盘蓄力/便宜期权）」，以 VIX 为核心中枢。"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from . import db, market

DEFAULT_WATCHLIST = ["VIX", "NVDA", "NDX", "SPX", "GOLD", "BTC"]


def _stddev(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / (len(vals) - 1)
    return math.sqrt(max(var, 0.0))


def _percentile_rank(series: list[float], value: float) -> float:
    if not series:
        return 50.0
    le = sum(1 for x in series if x <= value)
    return round((le / len(series)) * 100.0, 1)


def scan_volatility_radar(
    conn: sqlite3.Connection,
    symbols: list[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """扫描核心标的 + VIX 近 60 个交易日，双向识别：
    1) 波动率极端放大（Expansion：高 σ / VIX 急升或高位）→ 强推反转/超跌非对称下注
    2) 波动率极致收窄（Squeeze：VIX 处于低分位或近 5 日实现波动率压缩至 60 日均值 0.58× 以下）→ 强推变盘前埋伏/便宜期权
    """
    watch = symbols or DEFAULT_WATCHLIST
    items: list[dict[str, Any]] = []
    vix_panel: dict[str, Any] | None = None

    for raw in watch:
        sym, _, label = market.normalize_symbol(raw)
        try:
            data = market.get_candles(conn, sym, "1d", force=force)
        except market.MarketError:
            continue
        bars = data["candles"]
        if len(bars) < 12:
            continue

        rets: list[float] = []
        ranges_pct: list[float] = []
        for i in range(1, len(bars)):
            prev_c = bars[i - 1]["close"]
            if prev_c:
                rets.append(((bars[i]["close"] - prev_c) / prev_c) * 100.0)
            if bars[i]["close"]:
                ranges_pct.append(((bars[i]["high"] - bars[i]["low"]) / bars[i]["close"]) * 100.0)
        if len(rets) < 8:
            continue

        base_window = rets[-65:-5] if len(rets) >= 25 else rets[:-1]
        sd60 = max(_stddev(base_window), 0.35)
        rv5 = _stddev(rets[-5:])
        # 波动率压缩比：< 0.58 表示近 5 日振幅/波动极度收窄（变盘蓄力）
        squeeze_ratio = round(rv5 / sd60, 2) if sd60 else 1.0
        # 5日平均日内振幅 / 60日平均日内振幅
        avg_rng60 = sum(ranges_pct[-60:]) / max(1, len(ranges_pct[-60:]))
        avg_rng5 = sum(ranges_pct[-5:]) / max(1, len(ranges_pct[-5:]))
        range_squeeze = round(avg_rng5 / avg_rng60, 2) if avg_rng60 else 1.0
        eff_squeeze = round(min(squeeze_ratio, range_squeeze), 2)

        day_pct = round(rets[-1], 2)
        day_sigma = round(abs(day_pct) / sd60, 2)

        wk_ref = bars[-6]["close"] if len(bars) >= 6 else bars[0]["close"]
        wk_pct = round(((bars[-1]["close"] - wk_ref) / wk_ref) * 100.0, 2) if wk_ref else 0.0
        wk_sigma = round(abs(wk_pct) / (sd60 * math.sqrt(5)), 2)

        prev_wk_pct = 0.0
        if len(bars) >= 7 and bars[-7]["close"]:
            prev_wk_pct = round(((bars[-2]["close"] - bars[-7]["close"]) / bars[-7]["close"]) * 100.0, 2)
        prev_wk_sigma = round(abs(prev_wk_pct) / (sd60 * math.sqrt(5)), 2)

        peak_sigma = round(max(day_sigma, wk_sigma, prev_wk_sigma), 2)
        is_extreme = peak_sigma >= 2.2 or abs(day_pct) >= 4.0 or abs(prev_wk_pct) >= 7.0
        is_squeezed = (not is_extreme) and (eff_squeeze <= 0.58)

        item = {
            "symbol": sym,
            "label": data.get("label") or label,
            "lastPrice": bars[-1]["close"],
            "dayPct": day_pct,
            "daySigma": day_sigma,
            "weekPct": wk_pct,
            "prevWeekPct": prev_wk_pct,
            "baseDailyVol": round(sd60, 2),
            "peakSigma": peak_sigma,
            "squeezeRatio": eff_squeeze,
            "isExtreme": is_extreme,
            "isElevated": peak_sigma >= 1.5,
            "isSqueezed": is_squeezed,
        }

        if sym == "VIX":
            closes60 = [b["close"] for b in bars[-60:]]
            vix_pctile = _percentile_rank(closes60, bars[-1]["close"])
            vix_state = (
                "expansion"
                if (bars[-1]["close"] >= 24.0 or wk_pct >= 25.0 or prev_wk_pct >= 25.0 or (day_pct >= 12.0 and day_sigma >= 2.2))
                else ("compression" if (bars[-1]["close"] <= 14.5 or vix_pctile <= 15.0 or eff_squeeze <= 0.58) else "normal")
            )
            vix_panel = {
                "price": bars[-1]["close"],
                "dayPct": day_pct,
                "weekPct": wk_pct,
                "prevWeekPct": prev_wk_pct,
                "percentile60d": vix_pctile,
                "low60d": round(min(closes60), 2),
                "high60d": round(max(closes60), 2),
                "squeezeRatio": eff_squeeze,
                "peakSigma": peak_sigma,
                "state": vix_state,
                "stateLabel": {
                    "expansion": "🔥 VIX 高位放大 · 剧烈变盘/反转窗口",
                    "compression": "🧊 VIX 极致收窄/冰点 · 变盘前兆（期权极便宜）",
                    "normal": "🟢 VIX 常态区间",
                }[vix_state],
            }
            item["percentile60d"] = vix_pctile
            item["vixState"] = vix_state

        items.append(item)

    # 非 VIX 资产按异动度排序
    non_vix = sorted([x for x in items if x["symbol"] != "VIX"], key=lambda x: x["peakSigma"], reverse=True)
    vix_item = next((x for x in items if x["symbol"] == "VIX"), None)
    ordered_items = ([vix_item] if vix_item else []) + non_vix

    extreme_hits = [x for x in non_vix if x["isExtreme"]]
    elevated_hits = [x for x in non_vix if x["isElevated"]]
    squeezed_hits = [x for x in non_vix if x["isSqueezed"]]

    vix_exp = bool(vix_panel and vix_panel["state"] == "expansion")
    vix_comp = bool(vix_panel and vix_panel["state"] == "compression")

    if len(extreme_hits) >= 2 or (extreme_hits and extreme_hits[0]["peakSigma"] >= 3.0) or vix_exp:
        regime = "extreme"
        regime_label = "🔥 波动率极端放大 · 大机会日（博弈反转）"
    elif vix_comp or len(squeezed_hits) >= 2:
        regime = "squeeze"
        regime_label = "🧊 波动率极致收窄 · 变盘前兆（埋伏便宜期权）"
    elif len(elevated_hits) >= 2 or extreme_hits:
        regime = "elevated"
        regime_label = "⚡ 波动率放大中 · 重点盯盘窗口"
    else:
        regime = "calm"
        regime_label = "🟢 常规波动 · 耐心等待"

    vix_brief = (
        f"VIX 现报 {vix_panel['price']}（60日 {vix_panel['percentile60d']}% 分位 · 上周 {vix_panel['prevWeekPct']:+.1f}% · 压缩比 {vix_panel['squeezeRatio']}×）"
        if vix_panel
        else ""
    )
    top_names = "、".join(
        f"{x['symbol']}（日 {x['dayPct']:+.1f}% / 上周 {x['prevWeekPct']:+.1f}% · {x['peakSigma']}σ）"
        for x in non_vix[:3]
    )
    sq_names = "、".join(f"{x['symbol']}（压缩至 {x['squeezeRatio']}×）" for x in squeezed_hits[:3])

    if regime == "extreme":
        opening = (
            f"🔥 **波动率极端放大警报！** {vix_brief}；核心资产异动：{top_names}。\n\n"
            "这是最罕见的**波动率放大 · 大机会日**——先花 2 分钟回答我：\n"
            "1. 这波剧烈波动背后真正发生的核心逻辑是什么？是恐慌错杀超跌，还是新趋势起点？\n"
            "2. **We must find something to do.** 趁大波动高弹性窗口，如果只用可控小仓位（最大亏 0.5R 或固定小额美金）去买反转 Call/Put 或现货，你最想押哪一笔？"
        )
    elif regime == "squeeze":
        opening = (
            f"🧊 **波动率极致收窄 · 变盘前兆警报！** {vix_brief}"
            + (f"；盘面极度收窄：{sq_names}" if sq_names else "")
            + "。\n\n"
            "波动率压得越紧，后面的单边变盘往往越猛——而且**现在买期权（IV）正处于最便宜的甜点区**！先回答我两个问题：\n"
            "1. 你盯到接下来 1–2 周有什么潜在催化（CPI / 议息 / 财报 / 政策会议 / 关键均线三角末端）吗？\n"
            "2. **不要等大阳线/大阴线拉出来再追！** 趁现在波动率便宜，你要提前埋伏单边突破 Call/Put，还是用小额 Strangle（双买）锁死最大亏损去赌大变盘？"
        )
    elif regime == "elevated":
        opening = (
            f"⚡ **波动率正在抬升**：{vix_brief}；{top_names}。\n\n"
            "盘面开始脱离沉闷区间，你看到值得提前准备预案的大机会信号了吗？"
        )
    else:
        opening = (
            f"当前波动率处于常规中段（{vix_brief}；{top_names or '盘面平稳'}），既未到极端放大，也尚未收窄至冰点。\n\n"
            "你观察到有什么还没反映在价格里的潜在大催化吗？如果没有，今天留足子弹等下一次 VIX 冰点收窄或 3σ 爆发。"
        )

    return {
        "scannedAt": db.now_iso(),
        "regime": regime,
        "regimeLabel": regime_label,
        "extremeCount": len(extreme_hits),
        "squeezedCount": len(squeezed_hits),
        "vix": vix_panel,
        "openingQuestion": opening,
        "items": ordered_items,
    }
