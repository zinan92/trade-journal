#!/usr/bin/env python3
"""Reproducible showcase screenshot generator for Trade Journal README.

Runs an isolated Trade Journal server against a temporary demo SQLite database
(reusing only public OHLCV candles from local cache if present, never personal
journal entries) and captures verified screenshots into docs/screenshots/.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trade_journal import app as app_mod, db  # noqa: E402

OUT_DIR = ROOT / "docs" / "screenshots"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _seed_demo_db(demo_db: Path) -> None:
    conn = db.connect(demo_db)
    db.init_db(conn)

    # 仅复用本地公开行情缓存（candles / cache_meta），绝不复制个人复盘、计划或截图
    src_db = Path.home() / ".trade-journal" / "journal.db"
    if src_db.is_file():
        src = sqlite3.connect(str(src_db))
        candles = src.execute("SELECT symbol, interval, ts, open, high, low, close, volume FROM candles").fetchall()
        metas = src.execute("SELECT symbol, interval, yahoo_sym, currency, exchange, bar_count, fetched_at FROM cache_meta").fetchall()
        src.close()
        if candles:
            conn.executemany(
                "INSERT OR REPLACE INTO candles(symbol, interval, ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?)",
                candles,
            )
        if metas:
            now = db.now_iso()
            conn.executemany(
                "INSERT OR REPLACE INTO cache_meta(symbol, interval, yahoo_sym, currency, exchange, bar_count, fetched_at) VALUES (?,?,?,?,?,?,?)",
                [(m[0], m[1], m[2], m[3], m[4], m[5], now) for m in metas],
            )

    today = db.today_str()
    now = db.now_iso()
    ym = today[:7]
    d1 = f"{ym}-18"
    d2 = f"{ym}-21"
    d3 = today

    # Demo 盘前计划（覆盖 3 天，展示完整的每日 Package 日历）
    demo_plans = [
        (
            d1, "watch", "等 SMH 4h 回踩 EMA50 企稳再试多",
            "SPX 周线回踩 EMA50，NDX 短线偏弱但未破周线趋势，整体维持逢低观察。",
            "SMH 与存储链领跌，XLV / SCHD 防御板块收红，资金明显切向防守。",
            "今日优先观察 NVDA / MU 15m 企稳信号；若跌破前低则 Do Nothing 保留现金。",
            json.dumps({"vixPrice": 24.6, "medianPct": -2.35, "extremeDaysInYear": 18, "tradingDaysInYear": 252, "regime": "extreme"}, ensure_ascii=False),
            now,
        ),
        (
            d2, "do_nothing", "Do Nothing：波动收敛，留子弹等催化",
            "美股三大指数缩量窄幅震荡，黄金与 BTC 同步横盘，缺乏单边驱动。",
            "五大板块 ETF 涨跌均在 ±0.6% 以内，无明显领涨主线。",
            "今日 Do Nothing（空仓观望），不参与垃圾波动，把子弹留给下一次极端波动窗口。",
            json.dumps({"vixPrice": 15.8, "medianPct": 0.24, "extremeDaysInYear": 120, "tradingDaysInYear": 252, "regime": "calm"}, ensure_ascii=False),
            now,
        ),
        (
            d3, "bet", "VIX 站上 27+ 高位，小仓位锁风险博弈超跌反弹",
            "SPX / NDX 周线测试 EMA50 关键支撑，VIX 处于 60 日 95%+ 高分位，市场情绪显著恐慌。",
            "SMH 与存储/光通信板块跌幅居前，XLV 与 SCHD 逆势走强，风险偏好短期收缩。",
            "控制最大亏损在 0.5R 以内，观察 NVDA / SMH 15m 放量止跌结构，分批试探高赔率 Call；若未出企稳 K 线则继续观望。",
            json.dumps({"vixPrice": 27.1, "medianPct": -1.18, "extremeDaysInYear": 65, "tradingDaysInYear": 252, "regime": "extreme"}, ensure_ascii=False),
            now,
        ),
    ]
    for p in demo_plans:
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_plans(
                plan_date, stance, plan_note, step1_note, step2_note, step3_note,
                market_snapshot_json, focus_symbols_json, plan_followed, evening_review,
                signed_at, reviewed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', 'yes', '', ?, ?, ?)
            """,
            (p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[7], p[7]),
        )

    # Demo 盘后复盘文章（公开示例，无个人隐私）
    demo_entries = [
        (
            d3, "VIX 高位恐慌日：守住纪律等 15m 企稳", "NVDA", "15m", "win", "+$1,240",
            json.dumps(["完美执行", "等待确认", "分批止盈"], ensure_ascii=False),
            "今天盘前看到 VIX 还在高位，半导体和存储领跌，但 XLV 防御走强。开盘前 30 分钟没有急着抄底，等 15m 走出放量下影线收回 EMA20 后小仓位试多 NVDA，最大风险锁死在 0.5R，尾盘反弹兑现大部分利润。",
            "VIX 维持 27+ 高位（60 日 95%+ 分位），美股半导体与存储板块经历连续回调，市场情绪偏恐慌但指数接近周线 EMA50 支撑位。",
            "开盘观望前两根 15m 阴线；在放量长下影线收回 15m EMA20 时按计划小仓位进场，止损设在前低下方（风险 0.5R），反弹至前高压力位减仓 70%，最终实现 +$1,240。",
            "1) 严格按盘前 Step 3 计划等 15m 企稳信号，没有在开盘急跌时接飞刀；\n2) 提前写死了最大亏损上限，持仓全程心态非常平静。",
            "尾盘最后 30% 仓位在接近 EMA50 压力时有点贪心，本应顺手把移动止损抬到成本上方锁住全部浮盈。",
            "极端恐慌日只用锁死上限的小仓位出手，进场只认 15m 结构确认，绝不在左侧盲接飞刀。",
            json.dumps(["NVDA", "SMH", "SPX", "NDX", "MU", "GOLD"], ensure_ascii=False),
        ),
        (
            d2, "窄幅无催化震荡日：果断 Do Nothing 留子弹", "SPX", "15m", "review", "$0",
            json.dumps(["完美执行", "耐心等待"], ensure_ascii=False),
            "今天盘前看周线和板块 ETF 都没方向，直接定了 Do Nothing，全天没手痒乱开单。",
            "三大指数与五大板块 ETF 全天窄幅波动在 ±0.6% 内，VIX 回落至常态中段，缺乏宏观或财报催化。",
            "全天零交易，按盘前计划执行 Do Nothing，把精力留给复盘和整理自选列表。",
            "顶住了盘中几次假突破诱惑，没有在垃圾波动里损耗本金和注意力。",
            "无明显失误；下次类似平静日可以更早关掉分时图，避免无效盯盘。",
            "没有清晰赔率与催化时，Do Nothing 本身就是最高胜率的交易决策。",
            json.dumps(["SPX", "QQQ", "SMH"], ensure_ascii=False),
        ),
        (
            d1, "半导体超跌反转：看懂逻辑却因怕回撤踏空", "MU", "15m", "missed", "$0",
            json.dumps(["踏空犹豫", "怕回撤", "不敢下注"], ensure_ascii=False),
            "盘前已经判断存储和半导体到了超跌位，但盘中看到波动太大犹豫没敢下单，错过后面一波 +8% 反转。",
            "AI 存储链连续回调，35 只自选池中位数单日下跌超过 2.3%，VIX 跃升至 24.6，恐慌盘集中出清。",
            "未实际成交。事后测算若在 15m 双底确认时以 0.4R 风险试多，盈亏比可达 3.5:1。",
            "盘前准确识别了板块分化和关键周线支撑区，没有在下跌中继乱抄底。",
            "15m 企稳信号已经出现，但因为害怕盘中剧烈震荡扫止损，在下单前反复犹豫最终完全空仓踏空。",
            "逻辑清晰且风险已缩到 0.5R 以内时，必须果断按计划扣扳机——小仓位试错远胜于看对却空仓。",
            json.dumps(["MU", "NVDA", "SMH", "NDX"], ensure_ascii=False),
        ),
    ]
    for e in demo_entries:
        conn.execute(
            """
            INSERT INTO entries(
                trade_date, title, symbol, interval, outcome, pnl,
                bias_tags_json, raw_note, context_note, execution_note,
                did_right_note, should_have_note, takeaway_note, symbols_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*e, now, now),
        )

    # Demo 刚性交易铁律
    demo_rules = [
        ("NVDA", "极端波动日只用锁死上限的小仓位（≤0.5R）出手，绝不因怕回撤空仓旁观。", 5),
        ("SPX", "没有清晰催化与赔率优势时，果断写下 Do Nothing 留足子弹。", 4),
        ("MU", "进场只认 15m 结构企稳确认，绝不在左侧急跌中盲接飞刀。", 3),
    ]
    for sym, text, hits in demo_rules:
        conn.execute(
            "INSERT INTO rules(entry_id, symbol, rule_text, category, hit_count, created_at) VALUES (NULL, ?, ?, '执行纪律', ?, ?)",
            (sym, text, hits, now),
        )

    # Demo 波动教练开场白（带明确时效戳）
    coach_open = (
        f"🕒 时效：Last updated: {now.replace('T', ' ')[:19]} · 美股日线截至 {d3}\n\n"
        "🔥 **极端波动警报（与左侧体温计实时同步）**\n"
        "VIX 现报 **27.1**（60日 95%+ 分位）；自选 35 只股票最新交易日出现显著分化。\n\n"
        "高弹性窗口已经打开，花 2 分钟拍板：\n"
        "1. 这波波动背后核心逻辑是什么？恐慌错杀还是趋势破位？\n"
        "2. 如果把最大亏损锁死在 **0.5R**，你准备等哪一只标的的 **15m 企稳信号** 出手？"
    )
    conn.execute(
        "INSERT INTO coach_messages(session_day, role, content, regime, created_at) VALUES (?, 'agent', ?, 'extreme', ?)",
        (d3, coach_open, now),
    )
    conn.commit()


def _optimize_size(path: Path) -> None:
    """Keep screenshots crisp and under ~550KB for fast GitHub rendering."""
    if path.stat().st_size <= 550 * 1024:
        return
    # Resize slightly if huge PNG
    if shutil.which("sips"):
        subprocess.run(["sips", "-Z", "1600", str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="tj-demo-shots-"))
    os.environ["TRADE_JOURNAL_DATA"] = str(tmp_dir)
    demo_db = tmp_dir / "journal.db"
    _seed_demo_db(demo_db)

    port = 18799
    fastapi_app = app_mod.create_app(demo_db)
    config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    time.sleep(1.2)

    base = f"http://127.0.0.1:{port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROME_BIN, headless=True)
            ctx = browser.new_context(viewport={"width": 1360, "height": 880}, device_scale_factor=1.5)
            page = ctx.new_page()

            # 1. Hero: Tab 1 Pre-trading (1wk + 4-EMA + 2-Card Thermometer + Step 1)
            page.goto(base, wait_until="networkidle")
            page.wait_for_selector(".m-card canvas", timeout=20000)
            time.sleep(1.2)
            hero_path = OUT_DIR / "hero.png"
            page.screenshot(path=str(hero_path))
            _optimize_size(hero_path)

            # 2. Full-page Pre-trading (folded in <details>)
            page.evaluate("window.scrollTo(0, 700)")
            time.sleep(0.5)
            page.evaluate("window.scrollTo(0, 1500)")
            time.sleep(0.5)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.5)
            full_path = OUT_DIR / "01-pretrading-full.png"
            page.screenshot(path=str(full_path), clip={"x": 0, "y": 0, "width": 1360, "height": 2200}, full_page=True)
            _optimize_size(full_path)

            # 3. Pre-trading 4h continuous view + Step 1 inline auto-save note
            page.click('#morningIvSeg [data-miv="4h"]')
            page.wait_for_selector(".m-card canvas", timeout=20000)
            time.sleep(1.5)
            page.evaluate("window.scrollTo(0, 420)")
            time.sleep(0.7)
            p_4h = OUT_DIR / "01-pretrading-4h.png"
            page.screenshot(path=str(p_4h))
            _optimize_size(p_4h)

            # 4. Pre-trading Slide-out Volatility Coach Drawer
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.4)
            page.click("[data-open-coach]")
            page.wait_for_selector("#coachDrawer:not(.hidden)", timeout=10000)
            time.sleep(0.8)
            p_coach = OUT_DIR / "02-pretrading-coach.png"
            page.screenshot(path=str(p_coach))
            _optimize_size(p_coach)
            page.click("#btnCoachClose")
            time.sleep(0.3)

            # 5. Tab 2: After-trading Editorial Article Review (with Pre-trading snapshot + Did Right + #hashtags + thumbnails)
            page.click('.nav-btn[data-view="journal"]')
            page.wait_for_selector("#articlePlanCard:not(.hidden)", timeout=10000)
            page.wait_for_selector(".inline-chart-thumb canvas", timeout=15000)
            time.sleep(1.2)
            p_art = OUT_DIR / "03-aftertrading-article.png"
            page.screenshot(path=str(p_art))
            _optimize_size(p_art)

            # 6. Zoom Modal with 4-EMA + hollow green candles + 4-type Marker Pinning
            page.click('.inline-chart-thumb[data-ithumb="NVDA"]')
            page.wait_for_selector("#zoomModal:not(.hidden)", timeout=10000)
            page.wait_for_selector("#zoomChartEl canvas", timeout=15000)
            time.sleep(1.0)
            p_zoom = OUT_DIR / "04-chart-zoom-pin.png"
            page.screenshot(path=str(p_zoom))
            _optimize_size(p_zoom)
            page.click("#btnZoomClose")
            time.sleep(0.3)

            # 7. Tab 3: Monthly Daily Package Calendar + Rules & Stats
            page.click('.nav-btn[data-view="rules"]')
            page.wait_for_selector("#calendarGrid .cal-cell", timeout=10000)
            time.sleep(0.8)
            p_cal = OUT_DIR / "05-calendar-packages.png"
            page.screenshot(path=str(p_cal))
            _optimize_size(p_cal)

            # 8. Data / Cache Modal + Empty Day State (showing transparent offline/empty contract)
            # Click an empty calendar day first, then open Cache/CSV Modal
            page.click('#calendarGrid .cal-cell[data-cdate$="-05"]')
            time.sleep(0.4)
            p_empty = OUT_DIR / "06-empty-day-state.png"
            page.screenshot(path=str(p_empty))
            _optimize_size(p_empty)

            page.click("#btnOpenCache")
            page.wait_for_selector("#cacheModal:not(.hidden)", timeout=5000)
            time.sleep(0.5)
            p_cache = OUT_DIR / "07-local-cache-csv.png"
            page.screenshot(path=str(p_cache))
            _optimize_size(p_cache)

            browser.close()
    finally:
        server.should_exit = True
        shutil.rmtree(tmp_dir, ignore_errors=True)

    for f in sorted(OUT_DIR.glob("*.png")):
        print(f"CAPTURED {f.name:28s} {f.stat().st_size // 1024:4d} KB")


if __name__ == "__main__":
    main()
