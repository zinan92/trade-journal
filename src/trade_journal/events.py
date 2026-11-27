"""市场关键事件日历与今日大波动预警引擎（假日/半天盘、FOMC、CPI/非农、核心财报、四巫日与自定义事件）。"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from . import db

VALID_CATEGORIES = ("holiday", "fomc", "macro", "earnings", "opex", "custom")
VALID_IMPACTS = ("high", "medium", "low")

CATEGORY_META: dict[str, dict[str, str]] = {
    "fomc": {"label": "美联储 FOMC", "icon": "🏦"},
    "macro": {"label": "宏观数据", "icon": "📈"},
    "earnings": {"label": "核心财报", "icon": "📊"},
    "holiday": {"label": "假日 / 半天盘", "icon": "🗓"},
    "opex": {"label": "期权到期", "icon": "⚡"},
    "custom": {"label": "自定义提醒", "icon": "📌"},
}

# 2026–2027 核心已确认/预计关键市场日期（美股交易日历口径 YYYY-MM-DD）
BUILTIN_EVENTS: list[dict[str, str]] = [
    # ---------- 2026 假日 & 半天盘 ----------
    {
        "event_date": "2026-11-26",
        "category": "holiday",
        "title": "感恩节（美股休市）",
        "impact": "medium",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股现货休市，CME 股指与贵金属期货提前休市，仅加密与亚盘交易。",
    },
    {
        "event_date": "2026-11-27",
        "category": "holiday",
        "title": "黑五半天盘（提前收盘）",
        "impact": "high",
        "time_label": "13:00 ET / 次日 02:00 北京收盘",
        "symbol": "SPX",
        "note": "仅交易 3.5 小时，流动性极薄、买卖价差偏宽；隔夜跳空后极易假突破与 IV Crush，忌开盘盲追短日期权。",
    },
    {
        "event_date": "2026-12-24",
        "category": "holiday",
        "title": "平安夜半天盘（提前收盘）",
        "impact": "medium",
        "time_label": "13:00 ET / 次日 02:00 北京收盘",
        "symbol": "SPX",
        "note": "圣诞前夕半天盘，机构休假、成交清淡，适合轻仓或 Do Nothing。",
    },
    {
        "event_date": "2026-12-25",
        "category": "holiday",
        "title": "圣诞节（美股休市）",
        "impact": "medium",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股及欧美主要交易所休市。",
    },
    # ---------- 2027 美股主要假日 & 半天盘 ----------
    {
        "event_date": "2027-01-01",
        "category": "holiday",
        "title": "元旦（美股休市）",
        "impact": "medium",
        "time_label": "全天休市",
        "symbol": "",
        "note": "新年休市，节后首周关注机构新年调仓资金流。",
    },
    {
        "event_date": "2027-01-18",
        "category": "holiday",
        "title": "马丁·路德·金纪念日（美股休市）",
        "impact": "low",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股休市，周二恢复交易。",
    },
    {
        "event_date": "2027-02-15",
        "category": "holiday",
        "title": "总统日（美股休市）",
        "impact": "low",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股休市。",
    },
    {
        "event_date": "2027-03-26",
        "category": "holiday",
        "title": "耶稣受难日（美股休市）",
        "impact": "low",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股休市。",
    },
    {
        "event_date": "2027-05-31",
        "category": "holiday",
        "title": "阵亡将士纪念日（美股休市）",
        "impact": "low",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股休市。",
    },
    {
        "event_date": "2027-06-18",
        "category": "holiday",
        "title": "六月节调休（美股休市）",
        "impact": "low",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股休市。",
    },
    {
        "event_date": "2027-07-05",
        "category": "holiday",
        "title": "美国独立日调休（美股休市）",
        "impact": "low",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股休市。",
    },
    {
        "event_date": "2027-09-06",
        "category": "holiday",
        "title": "劳工节（美股休市）",
        "impact": "low",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股休市。",
    },
    {
        "event_date": "2027-11-25",
        "category": "holiday",
        "title": "感恩节（美股休市）",
        "impact": "medium",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股休市。",
    },
    {
        "event_date": "2027-11-26",
        "category": "holiday",
        "title": "黑五半天盘（提前收盘）",
        "impact": "high",
        "time_label": "13:00 ET 提前收盘",
        "symbol": "SPX",
        "note": "假日半天盘，流动性极薄，防假突破。",
    },
    {
        "event_date": "2027-12-24",
        "category": "holiday",
        "title": "圣诞节调休（美股休市）",
        "impact": "medium",
        "time_label": "全天休市",
        "symbol": "",
        "note": "美股休市。",
    },
    # ---------- 2026–2027 美联储 FOMC 利率决议日（官方日程） ----------
    {
        "event_date": "2026-12-09",
        "category": "fomc",
        "title": "FOMC 利率决议 + 点阵图 (SEP)",
        "impact": "high",
        "time_label": "14:00 ET / 次日 03:00 北京",
        "symbol": "SPX",
        "note": "全年收官议息 + 最新经济预测与点阵图，14:00–15:00 ET 极易双向剧烈扫盘；叠加 AVGO 盘后财报，超级大波动日。",
    },
    {
        "event_date": "2027-01-27",
        "category": "fomc",
        "title": "FOMC 利率决议 + 主席发布会",
        "impact": "high",
        "time_label": "14:00 ET / 次日 03:00 北京",
        "symbol": "SPX",
        "note": "2027 首场议息，决定开年利率预期，14:00–15:00 ET 高波动窗口。",
    },
    {
        "event_date": "2027-03-17",
        "category": "fomc",
        "title": "FOMC 利率决议 + 点阵图 (SEP)",
        "impact": "high",
        "time_label": "14:00 ET / 次日 02:00 北京",
        "symbol": "SPX",
        "note": "季度点阵图更新，关注利率路径重定价。",
    },
    {
        "event_date": "2027-04-28",
        "category": "fomc",
        "title": "FOMC 利率决议",
        "impact": "high",
        "time_label": "14:00 ET / 次日 02:00 北京",
        "symbol": "SPX",
        "note": "美联储利率决议与新闻发布会。",
    },
    {
        "event_date": "2027-06-09",
        "category": "fomc",
        "title": "FOMC 利率决议 + 点阵图 (SEP)",
        "impact": "high",
        "time_label": "14:00 ET / 次日 02:00 北京",
        "symbol": "SPX",
        "note": "年中点阵图更新，注意美债收益率与科技估值联动。",
    },
    {
        "event_date": "2027-07-28",
        "category": "fomc",
        "title": "FOMC 利率决议",
        "impact": "high",
        "time_label": "14:00 ET / 次日 02:00 北京",
        "symbol": "SPX",
        "note": "美联储利率决议与新闻发布会。",
    },
    {
        "event_date": "2027-09-15",
        "category": "fomc",
        "title": "FOMC 利率决议 + 点阵图 (SEP)",
        "impact": "high",
        "time_label": "14:00 ET / 次日 02:00 北京",
        "symbol": "SPX",
        "note": "秋季点阵图更新，叠加密集财报前瞻与季末调仓。",
    },
    {
        "event_date": "2027-10-27",
        "category": "fomc",
        "title": "FOMC 利率决议",
        "impact": "high",
        "time_label": "14:00 ET / 次日 02:00 北京",
        "symbol": "SPX",
        "note": "美联储利率决议与政策框架评估。",
    },
    {
        "event_date": "2027-11-17",
        "category": "fomc",
        "title": "FOMC 利率决议",
        "impact": "high",
        "time_label": "14:00 ET / 次日 03:00 北京",
        "symbol": "SPX",
        "note": "美联储利率决议与新闻发布会。",
    },
    # ---------- 关键宏观数据（BLS 官方日程） ----------
    {
        "event_date": "2026-12-04",
        "category": "macro",
        "title": "美国 11 月非农就业 (NFP)",
        "impact": "high",
        "time_label": "08:30 ET / 21:30 北京",
        "symbol": "SPX",
        "note": "12 月 FOMC 前最关键的就业数据，08:30 ET 公布瞬间股指期货与美债易大幅跳空。",
    },
    {
        "event_date": "2026-12-10",
        "category": "macro",
        "title": "美国 11 月 CPI 通胀数据",
        "impact": "high",
        "time_label": "08:30 ET / 21:30 北京",
        "symbol": "SPX",
        "note": "紧跟 FOMC 次日公布！若通胀高于预期极易引发二次剧烈洗盘。",
    },
    {
        "event_date": "2027-01-08",
        "category": "macro",
        "title": "美国 12 月非农就业 (NFP)",
        "impact": "high",
        "time_label": "08:30 ET / 21:30 北京",
        "symbol": "SPX",
        "note": "2027 开年首份非农就业报告。",
    },
    {
        "event_date": "2027-01-13",
        "category": "macro",
        "title": "美国 12 月 CPI 通胀数据",
        "impact": "high",
        "time_label": "08:30 ET / 21:30 北京",
        "symbol": "SPX",
        "note": "决定 1 月 FOMC 基调的核心通胀数据。",
    },
    # ---------- 核心公司财报（AI / 半导体 / 存储观察池锚点） ----------
    {
        "event_date": "2026-12-01",
        "category": "earnings",
        "title": "MRVL 迈威尔 Q3 财报",
        "impact": "high",
        "time_label": "盘后 AMC（已确认）",
        "symbol": "MRVL",
        "note": "ASIC 与高速光 DSP 需求风向标，直接联动 COHR / LITE / SMH。",
    },
    {
        "event_date": "2026-12-09",
        "category": "earnings",
        "title": "AVGO 博通 Q4 财报",
        "impact": "high",
        "time_label": "盘后 AMC（已确认）",
        "symbol": "SMH",
        "note": "AI 定制芯片与网络交换机核心龙头，且与 FOMC 决议同日，半导体高波动重头戏。",
    },
    {
        "event_date": "2026-12-10",
        "category": "earnings",
        "title": "TSM 台积电 11 月月度营收",
        "impact": "medium",
        "time_label": "盘前 / 亚洲时段",
        "symbol": "TSM",
        "note": "验证 AI 先进制程与 CoWoS 产能交付节奏。",
    },
    {
        "event_date": "2026-12-14",
        "category": "earnings",
        "title": "ORCL 甲骨文 Q2 财报",
        "impact": "medium",
        "time_label": "盘后 AMC（预计）",
        "symbol": "NVDA",
        "note": "云厂商 AI 资本开支（CapEx）与 GPU 订单情绪晴雨表。",
    },
    {
        "event_date": "2026-12-17",
        "category": "earnings",
        "title": "MU 美光科技 Q1 财报",
        "impact": "high",
        "time_label": "盘后 AMC（已确认）",
        "symbol": "MU",
        "note": "HBM / DRAM 存储周期总锚点，直接决定 Step 3B（MU / HYNIX / WDC / SNDK）方向。",
    },
    {
        "event_date": "2027-01-08",
        "category": "earnings",
        "title": "TSM 台积电 12 月及全年营收",
        "impact": "medium",
        "time_label": "盘前 / 亚洲时段",
        "symbol": "TSM",
        "note": "Q4 完整营收兑现度预览。",
    },
    {
        "event_date": "2027-01-21",
        "category": "earnings",
        "title": "TSM 台积电 Q4 财报",
        "impact": "high",
        "time_label": "盘前 BMO（预计）",
        "symbol": "TSM",
        "note": "2027 全年 CapEx 指引与 AI 芯片增速指引，牵动整个半导体设备与算力链。",
    },
    {
        "event_date": "2027-01-27",
        "category": "earnings",
        "title": "ASML 阿斯麦 Q4 财报",
        "impact": "high",
        "time_label": "盘前 BMO（已确认）",
        "symbol": "ASML",
        "note": "光刻机订单与半导体设备风向标（联动 AMAT / LRCX / EUV），且与 FOMC 同日。",
    },
    {
        "event_date": "2027-02-02",
        "category": "earnings",
        "title": "AMD Q4 财报",
        "impact": "high",
        "time_label": "盘后 AMC（预计）",
        "symbol": "AMD",
        "note": "AI GPU 市占率与数据中心增速检验，高弹性波动窗口。",
    },
    {
        "event_date": "2027-02-24",
        "category": "earnings",
        "title": "NVDA 英伟达 Q4 FY27 财报",
        "impact": "high",
        "time_label": "盘后 AMC（预计）",
        "symbol": "NVDA",
        "note": "全市场 AI 与半导体总锚点！财报前后 IV 极高、单日波动常达 ±6–10%，全盘风格定调日。",
    },
]


def _third_friday(year: int, month: int) -> date:
    """计算指定年月的第三个周五（美股期权到期日 / 季末四巫日）。"""
    cal = calendar.monthcalendar(year, month)
    fridays = [week[calendar.FRIDAY] for week in cal if week[calendar.FRIDAY] != 0]
    return date(year, month, fridays[2])


def _quad_witching_events(years: set[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for y in sorted(years):
        for m in (3, 6, 9, 12):
            d = _third_friday(y, m)
            dstr = d.isoformat()
            out.append(
                {
                    "id": f"builtin:opex:{dstr}",
                    "event_date": dstr,
                    "category": "opex",
                    "title": f"Q{m // 3} 四巫日 (Quad Witching)",
                    "impact": "high",
                    "time_label": "全天（尾盘放量）",
                    "symbol": "SPX",
                    "note": "股指期货、股指期权、个股期权与个股期货同日到期，开盘与尾盘 1 小时成交暴增、易现 Gamma 挤压或反转。",
                    "source": "builtin",
                    "editable": False,
                }
            )
    return out


def _decorate_event(raw: dict[str, Any]) -> dict[str, Any]:
    cat = raw.get("category") if raw.get("category") in VALID_CATEGORIES else "custom"
    meta = CATEGORY_META[cat]
    return {
        "id": str(raw["id"]),
        "db_id": raw.get("db_id"),
        "event_date": str(raw["event_date"]),
        "category": cat,
        "categoryLabel": meta["label"],
        "icon": meta["icon"],
        "title": str(raw["title"]),
        "impact": raw.get("impact") if raw.get("impact") in VALID_IMPACTS else "medium",
        "time_label": str(raw.get("time_label") or ""),
        "symbol": str(raw.get("symbol") or "").upper(),
        "note": str(raw.get("note") or ""),
        "source": str(raw.get("source") or "builtin"),
        "editable": bool(raw.get("editable", False)),
    }


def list_events(
    conn: sqlite3.Connection,
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """按日期区间 [start_date, end_date] 返回合并后的内置日历 + 自定义事件列表（按日期与重要度排序）。"""
    try:
        y1 = int(start_date[:4])
        y2 = int(end_date[:4])
    except ValueError:
        y1 = y2 = date.today().year
    years = set(range(min(y1, y2), max(y1, y2) + 1))

    items: list[dict[str, Any]] = []
    for idx, ev in enumerate(BUILTIN_EVENTS):
        if start_date <= ev["event_date"] <= end_date:
            items.append(
                _decorate_event(
                    {
                        **ev,
                        "id": f"builtin:{idx}:{ev['event_date']}",
                        "source": "builtin",
                        "editable": False,
                    }
                )
            )
    for ev in _quad_witching_events(years):
        if start_date <= ev["event_date"] <= end_date:
            items.append(_decorate_event(ev))

    rows = conn.execute(
        "SELECT id, event_date, category, title, impact, time_label, symbol, note, created_at "
        "FROM market_events WHERE event_date >= ? AND event_date <= ? ORDER BY event_date ASC, id ASC",
        (start_date, end_date),
    ).fetchall()
    for r in rows:
        items.append(
            _decorate_event(
                {
                    "id": f"custom:{r['id']}",
                    "db_id": r["id"],
                    "event_date": r["event_date"],
                    "category": r["category"],
                    "title": r["title"],
                    "impact": r["impact"],
                    "time_label": r["time_label"],
                    "symbol": r["symbol"],
                    "note": r["note"],
                    "source": "custom",
                    "editable": True,
                }
            )
        )

    impact_rank = {"high": 0, "medium": 1, "low": 2}
    cat_rank = {"holiday": 0, "fomc": 1, "macro": 2, "earnings": 3, "opex": 4, "custom": 5}
    items.sort(key=lambda x: (x["event_date"], impact_rank.get(x["impact"], 2), cat_rank.get(x["category"], 9), x["title"]))
    return items


def events_by_month(conn: sqlite3.Connection, ym: str) -> dict[str, list[dict[str, Any]]]:
    """返回指定月份 YYYY-MM 每天的事件映射。"""
    y, m = int(ym[:4]), int(ym[5:7])
    last_day = calendar.monthrange(y, m)[1]
    start_date = f"{y:04d}-{m:02d}-01"
    end_date = f"{y:04d}-{m:02d}-{last_day:02d}"
    grouped: dict[str, list[dict[str, Any]]] = {}
    for ev in list_events(conn, start_date=start_date, end_date=end_date):
        grouped.setdefault(ev["event_date"], []).append(ev)
    return grouped


def _format_days_away(days: int) -> str:
    if days == 0:
        return "今天"
    if days == 1:
        return "明天"
    if days == 2:
        return "后天"
    if days < 0:
        return f"{abs(days)} 天前"
    return f"{days} 天后"


def build_session_status(today_str: str, today_events: list[dict[str, Any]]) -> dict[str, Any]:
    """判断今日美股交易时段状态（休市 / 半天盘 / 周末 / 常规交易日）。"""
    d = date.fromisoformat(today_str)
    for ev in today_events:
        if ev["category"] == "holiday":
            if "半天" in ev["title"] or "提前收盘" in ev["time_label"]:
                return {
                    "kind": "half_day",
                    "label": f"⏳ {ev['title']} · {ev['time_label'] or '13:00 ET 提前收盘'}",
                    "shortLabel": ev["title"],
                    "note": ev["note"],
                }
            if "休市" in ev["title"] or "休市" in ev["time_label"]:
                return {
                    "kind": "closed",
                    "label": f"🛑 {ev['title']} · 美股现货休市",
                    "shortLabel": ev["title"],
                    "note": ev["note"],
                }
    if d.weekday() >= 5:
        return {
            "kind": "weekend",
            "label": "🌙 周末休市 · 仅加密货币交易",
            "shortLabel": "周末休市",
            "note": "美股休市，适合复盘周线与提前准备下周交易计划。",
        }
    return {
        "kind": "regular",
        "label": "🟢 常规美股交易日（09:30–16:00 ET）",
        "shortLabel": "常规交易日",
        "note": "",
    }


def build_catalyst_outlook(
    conn: sqlite3.Connection,
    *,
    today_str: str | None = None,
    vix: dict[str, Any] | None = None,
    thermometer: dict[str, Any] | None = None,
    horizon_days: int = 95,
) -> dict[str, Any]:
    """汇总今日大波动概率评估 + 今日特殊交易安排 + 未来关键催化倒计时。"""
    tday = today_str or db.today_str()
    d0 = date.fromisoformat(tday)
    end_d = (d0 + timedelta(days=horizon_days)).isoformat()
    all_ev = list_events(conn, start_date=tday, end_date=end_d)

    for ev in all_ev:
        diff = (date.fromisoformat(ev["event_date"]) - d0).days
        ev["daysAway"] = diff
        ev["countdown"] = _format_days_away(diff)

    today_events = [e for e in all_ev if e["daysAway"] == 0]
    upcoming_events = [e for e in all_ev if e["daysAway"] > 0]
    session = build_session_status(tday, today_events)

    # 综合打分（0..100）：回答「今天是否有可能迎来大波动？」
    score = 15
    reasons: list[str] = []

    vix_price = float(vix["price"]) if vix and vix.get("price") is not None else None
    vix_p60 = float(vix["percentile60d"]) if vix and vix.get("percentile60d") is not None else None
    vix_p1y = float(vix["percentile1y"]) if vix and vix.get("percentile1y") is not None else None
    vix_state = vix.get("state") if vix else None

    if vix_price is not None:
        if vix_state == "expansion" or vix_price >= 24.0:
            score += 42
            reasons.append(f"VIX 处极端高位 {vix_price}（1 年 {vix_p1y:.0f}% 分位），日内振幅与期权 IV 均处高危区")
        elif vix_state == "elevated" or vix_price >= 20.0:
            score += 24
            reasons.append(f"VIX 抬升至 {vix_price}（60 日 {vix_p60:.0f}% 分位），盘中弹性显著放大")
        elif vix_state == "compression":
            score += 18
            reasons.append(f"VIX 压至冰点 {vix_price}（压缩比 {vix.get('squeezeRatio')}×），低波蓄势易触发变盘突破")

    if thermometer and thermometer.get("totalCount"):
        sev = thermometer.get("severity")
        med = float(thermometer.get("medianPct") or 0.0)
        if sev == "extreme":
            score += 25
            reasons.append(
                f"昨日 35 只自选股中位数 {med:+.2f}%（1 年仅 {thermometer.get('extremeDaysInYear')}/{thermometer.get('tradingDaysInYear', 252)} 天），惯性/反抽波动极强"
            )
        elif sev == "elevated":
            score += 12
            reasons.append(f"昨日自选股中位数 {med:+.2f}%，市场广度已偏离常规区")

    high_today = [e for e in today_events if e["impact"] == "high"]
    if session["kind"] == "half_day":
        score += 18
        reasons.append(f"{session['shortLabel']}（仅交易 3.5 小时），流动性极薄，隔夜跳空后极易假突破与 IV Crush")
    elif session["kind"] == "closed":
        score = min(score, 25)
        reasons.append(f"{session['shortLabel']}，美股现货停盘，仅关注期货与加密货币情绪")

    for ev in high_today:
        if ev["category"] in ("fomc", "macro", "earnings", "opex", "custom"):
            score += 28
            reasons.append(f"今日重磅催化：{ev['title']}（{ev['time_label'] or '全天'}）— {ev['note'][:56]}")

    # 明日/未来 3 天内的高影响事件提醒（盘前提前定价/博弈）
    imminent = [e for e in upcoming_events if e["daysAway"] <= 4 and e["impact"] == "high"]
    if imminent:
        brief = "、".join(f"{e['countdown']} {e['title']}" for e in imminent[:2])
        score += 8
        reasons.append(f"临近催化预警：{brief}，盘中资金可能提前博弈或对冲")

    score = max(0, min(100, score))

    if session["kind"] in ("closed", "weekend"):
        level = "low"
        badge = "🌙 休市日 · 聚焦复盘与计划"
        headline = f"{session['shortLabel']}，美股现货不交易；可重点观察期货/加密动向并提前准备下周催化计划。"
    elif score >= 68:
        level = "extreme"
        badge = "🔥 今日大波动高概率"
        if session["kind"] == "half_day":
            headline = "高 VIX + 假日半天盘薄流动性叠加：开盘易大幅跳空且极易假突破/杀 IV，切忌追高，严控仓位或果断 Do Nothing。"
        elif any(e["category"] in ("fomc", "macro", "earnings") for e in high_today):
            headline = "高波动体制叠加今日重磅催化：双向剧烈扫盘概率极高，务必提前锁死最大亏损上限再出手。"
        else:
            headline = "VIX 与盘面广度均处于极端高弹区：今日日内振幅将显著大于平常，适合锁死亏损上限（≤0.5R）博弈非对称赔率。"
    elif score >= 45:
        level = "high"
        badge = "⚡ 警惕显著波动窗口"
        headline = "市场已脱离平静区或临近关键事件：开盘前明确触发价与止损，等 15m 结构确认再决定是否下注。"
    elif vix_state == "compression":
        level = "watch"
        badge = "🧊 低波蓄势 · 变盘前兆"
        headline = "波动率处于压缩冰点、期权便宜，留意近期催化触发方向性突破。"
    else:
        level = "low"
        badge = "🟢 常规波动日"
        headline = "今日无高危事件且波动平稳；若个股无清晰 4-EMA 信号，签字 Do Nothing 留足子弹就是好交易。"

    if not reasons:
        reasons.append("今日无高冲击宏观/财报事件，VIX 与自选广度处于常规区间。")

    # 精选未来催化（优先保留 25 天内全部事件 + 远期 95 天内高影响锚点如 FOMC / TSM / ASML / NVDA 财报）
    near = [e for e in upcoming_events if e["daysAway"] <= 25]
    far_anchor = [e for e in upcoming_events if e["daysAway"] > 25 and e["impact"] == "high"]
    spotlight = (near + far_anchor)[:18]

    return {
        "today": tday,
        "session": session,
        "bigMove": {
            "level": level,
            "score": score,
            "badge": badge,
            "headline": headline,
            "reasons": reasons[:4],
        },
        "todayEvents": today_events,
        "upcomingEvents": spotlight,
    }
