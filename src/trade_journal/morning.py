"""晨间 K 线快照引擎（Morning Desk）：
自上而下 4 层结构：
  0) 10 秒市场体温计：自选股中位数/平均涨跌幅 + 过去 1 年极端天数统计 + VIX 高低位仪表
  1) Step 1 · 大盘与宏观指数（标普/道指/纳指 + 黄金/白银/WTI + BTC/ETH）
  2) Step 2 · 核心板块 / 风格 ETF（SMH, QQQ, XLV, EWY, SCHD）
  3) Step 3 · 美股核心自选池（按 3 大类别全部铺开：AI芯片与设备 / 存储HBM与光通信 / 科技巨头与防御）
支持一键切换 周线 (1wk) → 4 小时 (4h) → 15 分钟 (15m) 全页肉眼扫图。
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median
from typing import Any

from . import db, events, market

MORNING_SECTIONS: list[dict[str, Any]] = [
    {
        "id": "indices",
        "step": "Step 1",
        "title": "大盘 & 跨资产宏观",
        "subtitle": "先看指数与商品/加密定大基调，再往下看板块与个股",
        "inBreadth": False,
        "items": [
            {"symbol": "SPX", "name": "标普 500", "subtag": "美股大盘"},
            {"symbol": "DJI", "name": "道琼斯", "subtag": "美股大盘"},
            {"symbol": "NDX", "name": "纳斯达克 100", "subtag": "科技大盘"},
            {"symbol": "GOLD", "name": "黄金期货", "subtag": "贵金属"},
            {"symbol": "SILVER", "name": "白银期货", "subtag": "贵金属"},
            {"symbol": "WTI", "name": "WTI 原油", "subtag": "能源"},
            {"symbol": "BTC", "name": "比特币", "subtag": "加密"},
            {"symbol": "ETH", "name": "以太坊", "subtag": "加密"},
        ],
    },
    {
        "id": "etfs",
        "step": "Step 2",
        "title": "核心板块 / 风格 ETF",
        "subtitle": "一眼判断全盘普涨普跌，还是半导体 / 成长 / 防御 / 韩国链分化轮动",
        "inBreadth": False,
        "items": [
            {"symbol": "SMH", "name": "半导体 ETF", "subtag": "AI硬件核心"},
            {"symbol": "QQQ", "name": "纳指 100 ETF", "subtag": "成长科技"},
            {"symbol": "XLV", "name": "医疗防御 ETF", "subtag": "防御轮动"},
            {"symbol": "EWY", "name": "韩国 ETF", "subtag": "存储外溢"},
            {"symbol": "SCHD", "name": "红利防守 ETF", "subtag": "红利防守"},
        ],
    },
    {
        "id": "ai_compute",
        "step": "Step 3A",
        "title": "AI 芯片、大模型与半导体设备",
        "subtitle": "对照同类别周线/4h 形态一致性：算力芯片 · 光刻设备 · AI 云与核电",
        "inBreadth": True,
        "items": [
            {"symbol": "NVDA", "name": "英伟达", "subtag": "GPU龙头"},
            {"symbol": "AMD", "name": "AMD 超威", "subtag": "AI芯片"},
            {"symbol": "TSM", "name": "台积电", "subtag": "晶圆代工"},
            {"symbol": "INTC", "name": "英特尔", "subtag": "晶圆/CPU"},
            {"symbol": "CBRS", "name": "Cerebras", "subtag": "AI芯片"},
            {"symbol": "ANT", "name": "Anthropic", "subtag": "大模型"},
            {"symbol": "MRVL", "name": "迈威尔", "subtag": "ASIC/光DSP"},
            {"symbol": "QCOM", "name": "高通", "subtag": "端侧芯片"},
            {"symbol": "NBIS", "name": "Nebius", "subtag": "AI算力云"},
            {"symbol": "ASML", "name": "阿斯麦", "subtag": "光刻机"},
            {"symbol": "AMAT", "name": "应用材料", "subtag": "半导体设备"},
            {"symbol": "LRCX", "name": "拉姆研究", "subtag": "刻蚀设备"},
            {"symbol": "EUV", "name": "EUV 光刻 ETF", "subtag": "光刻链"},
            {"symbol": "VST", "name": "Vistra", "subtag": "AI核电"},
            {"symbol": "SOX", "name": "费城半导体指数", "subtag": "板块指数"},
            {"symbol": "SOXX", "name": "半导体 ETF", "subtag": "板块对照"},
            {"symbol": "TQQQ", "name": "3x 纳指 ETF", "subtag": "高弹情绪"},
        ],
    },
    {
        "id": "memory_optical",
        "step": "Step 3B",
        "title": "存储 HBM & 光通信互连",
        "subtitle": "存储芯片与光模块高弹性分支，同框看联动强弱",
        "inBreadth": True,
        "items": [
            {"symbol": "MU", "name": "美光科技", "subtag": "存储/HBM"},
            {"symbol": "SNDK", "name": "闪迪 Sandisk", "subtag": "NAND存储"},
            {"symbol": "WDC", "name": "西部数据", "subtag": "HDD存储"},
            {"symbol": "HYNIX", "name": "SK 海力士", "subtag": "HBM龙头"},
            {"symbol": "DRAM", "name": "Memory ETF", "subtag": "存储ETF"},
            {"symbol": "COHR", "name": "Coherent", "subtag": "光模块"},
            {"symbol": "LITE", "name": "Lumentum", "subtag": "光模块"},
            {"symbol": "GLW", "name": "康宁", "subtag": "光纤材料"},
            {"symbol": "KORU", "name": "3x 韩国 ETF", "subtag": "韩国高弹"},
        ],
    },
    {
        "id": "mega_other",
        "step": "Step 3C",
        "title": "科技巨头、高弹成长与消费防御",
        "subtitle": "大科技权重股 + 独立周期/零售风向标 + 防御对照",
        "inBreadth": True,
        "items": [
            {"symbol": "AAPL", "name": "苹果", "subtag": "科技巨头"},
            {"symbol": "GOOG", "name": "谷歌", "subtag": "科技巨头"},
            {"symbol": "AMZN", "name": "亚马逊", "subtag": "云/电商"},
            {"symbol": "TSLA", "name": "特斯拉", "subtag": "高弹/具身"},
            {"symbol": "PLTR", "name": "Palantir", "subtag": "AI软件"},
            {"symbol": "HOOD", "name": "Robinhood", "subtag": "散户风险偏好"},
            {"symbol": "PDD", "name": "拼多多", "subtag": "中概平台"},
            {"symbol": "RKLB", "name": "Rocket Lab", "subtag": "商业航天"},
            {"symbol": "KO", "name": "可口可乐", "subtag": "消费防御"},
        ],
    },
]

# 每个周期下发的 K 线数量上限（确保 EMA200 充分预热，且支持向左拖动和 zoom out 看更长历史）
INTERVAL_BAR_LIMITS: dict[str, int] = {
    "1wk": 380,  # 约 7 年周线（预热 EMA200）
    "1d": 380,   # 约 1.5 年日线（预热 EMA200）
    "4h": 380,   # 约半年 4 小时线（预热 EMA200）
    "1h": 380,   # 约 2-3 个月 1 小时线
    "30m": 380,  # 约 1 个月 30 分钟线
    "15m": 380,  # 约 3-4 周 15 分钟线（预热 EMA200）
    "5m": 380,   # 约 5 个交易日 5 分钟线
}


def all_morning_symbols() -> list[str]:
    syms: list[str] = []
    for sec in MORNING_SECTIONS:
        for item in sec["items"]:
            if item["symbol"] not in syms:
                syms.append(item["symbol"])
    return syms


def breadth_pool_symbols() -> list[str]:
    syms: list[str] = []
    for sec in MORNING_SECTIONS:
        if sec.get("inBreadth"):
            for item in sec["items"]:
                if item["symbol"] not in syms:
                    syms.append(item["symbol"])
    return syms


def _stddev(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return math.sqrt(max(sum((x - m) ** 2 for x in vals) / (len(vals) - 1), 0.0))


def _pct_rank(series: list[float], val: float) -> float:
    if not series:
        return 50.0
    return round((sum(1 for x in series if x <= val) / len(series)) * 100.0, 1)


def _compute_vix_status(vix_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not vix_data or len(vix_data.get("candles") or []) < 2:
        return None
    bars = vix_data["candles"]
    closes = [b["close"] for b in bars]
    closes60 = closes[-60:]
    closes1y = closes[-252:]
    last_c = closes[-1]
    prev_c = closes[-2]
    day_pct = round(((last_c - prev_c) / prev_c) * 100.0, 2) if prev_c else 0.0
    wk_ref = closes[-6] if len(closes) >= 6 else closes[0]
    week_pct = round(((last_c - wk_ref) / wk_ref) * 100.0, 2) if wk_ref else 0.0

    pct60 = _pct_rank(closes60, last_c)
    pct1y = _pct_rank(closes1y, last_c)

    rets = [((closes[i] - closes[i - 1]) / closes[i - 1]) * 100.0 for i in range(1, len(closes)) if closes[i - 1]]
    sd60 = max(_stddev(rets[-65:-5] if len(rets) >= 25 else rets), 0.35)
    rv5 = _stddev(rets[-5:])
    squeeze_ratio = round(rv5 / sd60, 2) if sd60 else 1.0

    if last_c >= 24.0 or pct60 >= 85.0:
        state = "expansion"
        state_label = "🔥 高位恐慌 · 极端波动区"
    elif last_c <= 14.5 or pct60 <= 15.0 or squeeze_ratio <= 0.58:
        state = "compression"
        state_label = "🧊 低位冰点 · 期权极便宜（变盘前兆）"
    elif last_c >= 20.0 or pct60 >= 70.0:
        state = "elevated"
        state_label = "⚡ 偏高警戒 · 波动抬升中"
    else:
        state = "normal"
        state_label = "🟢 常态中位 · 波动平稳"

    return {
        "price": round(last_c, 2),
        "dayPct": day_pct,
        "weekPct": week_pct,
        "percentile60d": pct60,
        "percentile1y": pct1y,
        "low60d": round(min(closes60), 2),
        "high60d": round(max(closes60), 2),
        "low1y": round(min(closes1y), 2),
        "high1y": round(max(closes1y), 2),
        "squeezeRatio": squeeze_ratio,
        "state": state,
        "stateLabel": state_label,
    }


def _compute_breadth(daily_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """计算 Step 3 关注股票池：最新交易日中位数/平均涨跌幅 + 过去 1 年同等极端天数统计。"""
    pool = breadth_pool_symbols()
    # 按 UTC 日历日聚合每只股票的日涨跌幅
    date_returns: dict[str, dict[str, float]] = defaultdict(dict)
    sym_meta: dict[str, dict[str, Any]] = {}
    for sec in MORNING_SECTIONS:
        for item in sec["items"]:
            sym_meta[item["symbol"]] = {**item, "sectionId": sec["id"], "sectionTitle": sec["title"]}

    for sym in pool:
        data = daily_map.get(sym)
        if not data:
            continue
        bars = data.get("candles") or []
        for i in range(1, len(bars)):
            prev_c = bars[i - 1]["close"]
            cur_c = bars[i]["close"]
            if not prev_c:
                continue
            dstr = datetime.fromtimestamp(int(bars[i]["time"]), tz=timezone.utc).strftime("%Y-%m-%d")
            date_returns[dstr][sym] = ((cur_c - prev_c) / prev_c) * 100.0

    all_dates = sorted(date_returns.keys())
    if not all_dates:
        return {
            "latestDate": "",
            "totalCount": 0,
            "upCount": 0,
            "downCount": 0,
            "flatCount": 0,
            "medianPct": 0.0,
            "meanPct": 0.0,
            "tradingDaysInYear": 0,
            "extremeDaysInYear": 0,
            "extremePctRank": 50.0,
            "direction": "flat",
            "rarityHeadline": "暂无足够日线数据，请点击刷新行情",
            "severity": "normal",
            "topGainers": [],
            "topLosers": [],
            "groupStats": [],
        }

    # 仅保留覆盖率达到一半以上的交易日（过滤美股休市时仅韩国市场单独交易的半残日）
    available_syms = len([s for s in pool if s in daily_map])
    min_cov = max(1, min(available_syms, max(2, available_syms // 2)))
    valid_dates = [d for d in all_dates if len(date_returns[d]) >= min_cov][-252:]
    if not valid_dates:
        valid_dates = all_dates[-252:]

    latest_date = valid_dates[-1]
    latest_map = date_returns[latest_date]
    latest_vals = list(latest_map.values())
    med = round(float(median(latest_vals)), 2)
    avg = round(sum(latest_vals) / len(latest_vals), 2)
    up_cnt = sum(1 for v in latest_vals if v > 0.05)
    dn_cnt = sum(1 for v in latest_vals if v < -0.05)
    flat_cnt = len(latest_vals) - up_cnt - dn_cnt

    # 过去 1 年每日中位数序列，用来数「这种极端情况一年出现多少天」
    hist_medians = [float(median(date_returns[d].values())) for d in valid_dates]
    total_days = len(hist_medians)

    if med < -0.05:
        direction = "down"
        extreme_days = sum(1 for x in hist_medians if x <= med + 1e-9)
        pct_rank = round((extreme_days / total_days) * 100.0, 1)
        headline = f"过去 1 年 {total_days} 个交易日中，普跌达到或超过昨天的只有 {extreme_days} 天（最差 {pct_rank}%）"
    elif med > 0.05:
        direction = "up"
        extreme_days = sum(1 for x in hist_medians if x >= med - 1e-9)
        pct_rank = round((extreme_days / total_days) * 100.0, 1)
        headline = f"过去 1 年 {total_days} 个交易日中，普涨达到或超过昨天的只有 {extreme_days} 天（最强 {pct_rank}%）"
    else:
        direction = "flat"
        extreme_days = total_days
        pct_rank = 50.0
        headline = f"过去 1 年 {total_days} 个交易日中，昨日属于常规窄幅震荡日"

    if direction != "flat" and (extreme_days <= 12 or pct_rank <= 5.0):
        severity = "extreme"
    elif direction != "flat" and (extreme_days <= 30 or pct_rank <= 15.0):
        severity = "elevated"
    else:
        severity = "normal"

    ranked = sorted(
        [
            {
                "symbol": sym,
                "name": sym_meta.get(sym, {}).get("name", sym),
                "subtag": sym_meta.get(sym, {}).get("subtag", ""),
                "pct": round(val, 2),
                "price": daily_map[sym]["lastPrice"],
            }
            for sym, val in latest_map.items()
        ],
        key=lambda x: x["pct"],
    )
    top_losers = ranked[:4]
    top_gainers = list(reversed(ranked[-4:]))

    # 3 个个股大类的分组中位数/均值
    group_stats: list[dict[str, Any]] = []
    for sec in MORNING_SECTIONS:
        if not sec.get("inBreadth"):
            continue
        g_vals = [latest_map[it["symbol"]] for it in sec["items"] if it["symbol"] in latest_map]
        if not g_vals:
            continue
        group_stats.append(
            {
                "id": sec["id"],
                "step": sec["step"],
                "title": sec["title"],
                "count": len(g_vals),
                "upCount": sum(1 for v in g_vals if v > 0.05),
                "downCount": sum(1 for v in g_vals if v < -0.05),
                "medianPct": round(float(median(g_vals)), 2),
                "meanPct": round(sum(g_vals) / len(g_vals), 2),
            }
        )

    return {
        "latestDate": latest_date,
        "totalCount": len(latest_vals),
        "upCount": up_cnt,
        "downCount": dn_cnt,
        "flatCount": flat_cnt,
        "medianPct": med,
        "meanPct": avg,
        "tradingDaysInYear": total_days,
        "extremeDaysInYear": extreme_days,
        "extremePctRank": pct_rank,
        "direction": direction,
        "rarityHeadline": headline,
        "severity": severity,
        "topGainers": top_gainers,
        "topLosers": top_losers,
        "groupStats": group_stats,
    }


def build_morning_summary(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    allow_network: bool = True,
) -> dict[str, Any]:
    """晨间体温计总览：一次并发预热日线并算出 VIX + 中位数/均值 + 1年极端天数 + 板块快览。"""
    syms = ["VIX"] + all_morning_symbols()
    daily_map = market.prefetch_candles_batch(conn, syms, "1d", force=force, allow_network=allow_network)

    vix = _compute_vix_status(daily_map.get("VIX"))
    thermometer = _compute_breadth(daily_map)

    # 顺手给出 Step 1 指数和 Step 2 ETF 的最新日涨跌幅，便于顶部极速扫一眼
    quick_strip: list[dict[str, Any]] = []
    for sec in MORNING_SECTIONS[:2]:
        for item in sec["items"]:
            d = daily_map.get(item["symbol"])
            if not d:
                continue
            quick_strip.append(
                {
                    "sectionId": sec["id"],
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "subtag": item["subtag"],
                    "lastPrice": d["lastPrice"],
                    "changePct": d["changePct"],
                }
            )

    today = db.today_str()
    plan_row = conn.execute("SELECT * FROM daily_plans WHERE plan_date=?", (today,)).fetchone()
    plan = db.plan_to_dict(plan_row, today)
    streak = db.daily_plan_streak(conn)
    scanned_at = db.now_iso()

    # 统一判定当前市场状态（供体温计和波动教练共用，彻底消除两套口径打架）
    vix_state = vix["state"] if vix else "normal"
    t_sev = thermometer.get("severity", "normal")
    t_dir = thermometer.get("direction", "flat")
    if vix_state == "expansion" or t_sev == "extreme":
        regime = "extreme"
        regime_label = "🔥 极端波动 · 大机会日"
    elif vix_state == "compression":
        regime = "squeeze"
        regime_label = "🧊 波动率极致收窄 · 变盘前兆（期权便宜）"
    elif vix_state == "elevated" or t_sev == "elevated":
        regime = "elevated"
        regime_label = "⚡ 显著波动 · 重点盯盘窗口"
    else:
        regime = "calm"
        regime_label = "🟢 常规波动 · 耐心等待"

    latest_eq_date = thermometer.get("latestDate") or today
    fetched_times = sorted(
        [d.get("fetchedAt") for d in daily_map.values() if d and d.get("fetchedAt")]
    )
    last_updated_at = fetched_times[-1] if fetched_times else scanned_at
    vix_fetched = (daily_map.get("VIX") or {}).get("fetchedAt") or last_updated_at
    freshness = {
        "equityDate": latest_eq_date,
        "vixFetchedAt": vix_fetched,
        "lastUpdatedAt": last_updated_at,
        "scannedAt": scanned_at,
        "label": f"Last updated: {last_updated_at.replace('T', ' ')[:19]} · 美股日线截至 {latest_eq_date}",
    }

    catalyst_outlook = events.build_catalyst_outlook(
        conn,
        today_str=today,
        vix=vix,
        thermometer=thermometer,
    )

    return {
        "scannedAt": scanned_at,
        "lastUpdatedAt": last_updated_at,
        "today": today,
        "regime": regime,
        "regimeLabel": regime_label,
        "freshness": freshness,
        "vix": vix,
        "thermometer": thermometer,
        "catalysts": catalyst_outlook,
        "quickStrip": quick_strip,
        "sections": MORNING_SECTIONS,
        "plan": plan,
        "streak": streak,
    }


def build_coach_opening(summary: dict[str, Any]) -> str:
    """基于与晨间工作台完全一致的实时体温计，生成带明确时效戳的教练开场白。"""
    regime = summary.get("regime", "calm")
    fresh = (summary.get("freshness") or {}).get("label") or f"扫描于 {summary.get('scannedAt', '')}"
    t = summary.get("thermometer") or {}
    v = summary.get("vix")

    vix_txt = (
        f"VIX 现报 **{v['price']}**（日 {v['dayPct']:+.1f}% · 60日 {v['percentile60d']}% 分位）"
        if v
        else "VIX 暂缺"
    )
    med_txt = (
        f"自选 {t.get('totalCount', 0)} 只股票最新交易日（{t.get('latestDate', '--')}）中位数 **{t.get('medianPct', 0):+.2f}%** / 平均 {t.get('meanPct', 0):+.2f}%（{t.get('upCount', 0)} 涨 / {t.get('downCount', 0)} 跌；1 年罕见度：**{t.get('extremeDaysInYear', '--')}/{t.get('tradingDaysInYear', 252)} 天**）"
        if t.get("totalCount")
        else ""
    )
    stamp = f"🕒 时效：{fresh}\n\n"
    cat = summary.get("catalysts") or {}
    bm = cat.get("bigMove") or {}
    sess = cat.get("session") or {}
    today_evs = cat.get("todayEvents") or []
    upcoming_evs = (cat.get("upcomingEvents") or [])[:4]
    ev_lines: list[str] = []
    if sess.get("kind") in ("half_day", "closed", "weekend"):
        ev_lines.append(f"• 今日交易时段：**{sess.get('label')}**")
    for ev in today_evs:
        if ev.get("category") != "holiday":
            ev_lines.append(f"• 今日催化：**{ev['title']}**（{ev.get('time_label') or '全天'}）")
    if upcoming_evs:
        ev_lines.append(
            "• 近期关注：" + " · ".join(f"{e['countdown']} {e['title']}" for e in upcoming_evs[:3])
        )
    cat_block = (f"📅 **今日波动雷达（{bm.get('badge', '')}）**\n" + "\n".join(ev_lines) + "\n\n") if ev_lines else ""

    if regime == "extreme":
        return (
            stamp
            + cat_block
            + f"🔥 **极端波动警报（与左侧体温计实时同步）**\n{vix_txt}；{med_txt}。\n\n"
            + "这是过去一年里最罕见的高弹性窗口，花 2 分钟拍板：\n"
            + "1. 这波剧烈波动背后真正发生了什么？是恐慌错杀超跌，还是结构性破位？\n"
            + "2. **We must find something to do.** 如果只用锁死上限的小额亏损（如 0.5R 或固定小额美金）去博弈反转 Call/Put 或现货，你最想押哪一只？"
        )
    if regime == "squeeze":
        return (
            stamp
            + cat_block
            + f"🧊 **波动率极致收窄警报（变盘前兆）**\n{vix_txt}；{med_txt}。\n\n"
            + "现在隐含波动率处于冰点，**买期权正是最便宜的甜点区**。先回答两句：\n"
            + "1. 未来 1–2 周你盯到什么催化（CPI / 议息 / 财报 / 关键均线三角末端）？\n"
            + "2. 趁期权便宜，你想提前埋伏单边突破 Call/Put，还是用小额 Strangle 双买锁死亏损赌变盘？"
        )
    if regime == "elevated":
        return (
            stamp
            + cat_block
            + f"⚡ **显著波动提示**\n{vix_txt}；{med_txt}。\n\n"
            + "盘面已经脱离常规区，切 4h / 15m 看一眼你最关心的标的——今天是 **重点盯盘等 15m 信号**，还是继续 **Do Nothing**？"
        )
    return (
        stamp
        + cat_block
        + f"🟢 **当前为常规波动日**\n{vix_txt}；{med_txt}。\n\n"
        + "市场既没到极端恐慌，也没收窄到冰点。如果今天没看到清晰催化，**直接签字「Do Nothing」留足子弹**就是满分操作。"
    )


def build_morning_charts(
    conn: sqlite3.Connection,
    *,
    interval: str = "4h",
    section: str = "",
    force: bool = False,
    allow_network: bool = True,
) -> dict[str, Any]:
    """返回指定周期（4h / 1wk / 15m / 1d）的所有分组 K 线数据，用于整页网格肉眼扫图。"""
    iv = interval if interval in INTERVAL_BAR_LIMITS else "4h"
    bar_limit = INTERVAL_BAR_LIMITS[iv]

    target_sections = [s for s in MORNING_SECTIONS if not section or s["id"] == section]
    syms = [it["symbol"] for s in target_sections for it in s["items"]]
    data_map = market.prefetch_candles_batch(conn, syms, iv, force=force, allow_network=allow_network)

    # 如果还要标注昨日日线涨跌幅，顺便从本地日线缓存读取（不触发网络）
    daily_cache = market.prefetch_candles_batch(conn, syms, "1d", force=False, allow_network=False)

    sections_out: list[dict[str, Any]] = []
    for sec in target_sections:
        cards: list[dict[str, Any]] = []
        for item in sec["items"]:
            sym = item["symbol"]
            d = data_map.get(sym)
            d1 = daily_cache.get(sym)
            if not d or not d.get("candles"):
                cards.append(
                    {
                        "symbol": sym,
                        "name": item["name"],
                        "subtag": item["subtag"],
                        "available": False,
                        "lastPrice": None,
                        "dayPct": d1["changePct"] if d1 else None,
                        "barPct": None,
                        "posPct": None,
                        "barCount": 0,
                        "candles": [],
                    }
                )
                continue

            bars = d["candles"][-bar_limit:]
            lows = [b["low"] for b in bars]
            highs = [b["high"] for b in bars]
            lo, hi = min(lows), max(highs)
            last_c = bars[-1]["close"]
            pos_pct = round(((last_c - lo) / (hi - lo)) * 100.0, 1) if hi > lo else 50.0
            compact = [
                {
                    "time": b["time"],
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"],
                }
                for b in bars
            ]
            cards.append(
                {
                    "symbol": sym,
                    "name": item["name"],
                    "subtag": item["subtag"],
                    "available": True,
                    "lastPrice": d["lastPrice"],
                    "dayPct": d1["changePct"] if d1 else d["changePct"],
                    "barPct": d["changePct"],
                    "posPct": pos_pct,
                    "barCount": len(compact),
                    "candles": compact,
                }
            )
        sections_out.append(
            {
                "id": sec["id"],
                "step": sec["step"],
                "title": sec["title"],
                "subtitle": sec["subtitle"],
                "inBreadth": sec.get("inBreadth", False),
                "cards": cards,
            }
        )

    chart_times = sorted([d.get("fetchedAt") for d in data_map.values() if d and d.get("fetchedAt")])
    return {
        "interval": iv,
        "fetchedAt": db.now_iso(),
        "lastUpdatedAt": chart_times[-1] if chart_times else db.now_iso(),
        "sections": sections_out,
    }
