"""Trade Journal 自动化测试：Slash 命令解析、CSV 导入 SQLite 缓存、复盘 CRUD、K 线打点、规则与截图上传。"""
from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from trade_journal import app as app_mod, market


def test_slash_command_parser() -> None:
    p1 = market.parse_slash_command("/SPX")
    assert p1["action"] == "chart" and p1["symbol"] == "SPX" and p1["yahooSymbol"] == "^GSPC" and p1["interval"] == "15m"

    p2 = market.parse_slash_command("/QQQ 1h")
    assert p2["symbol"] == "QQQ" and p2["interval"] == "1h"

    p3 = market.parse_slash_command("/btc 4h")
    assert p3["symbol"] == "BTC" and p3["yahooSymbol"] == "BTC-USD" and p3["interval"] == "4h"

    p4 = market.parse_slash_command("/tv")
    assert p4["action"] == "tv"


def test_journal_crud_csv_cache_markers_and_attachments(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADE_JOURNAL_DATA", str(tmp_path / "tj_data"))
    client = TestClient(app_mod.create_app(tmp_path / "tj_data" / "journal.db"))

    # 1. CSV 导入自定义 K 线到本地 SQLite 并命中缓存读取
    csv_body = (
        "time,open,high,low,close,volume\n"
        "2026-11-20 09:30:00,7250,7265,7242,7260,12000\n"
        "2026-11-20 09:45:00,7260,7278,7255,7274,15400\n"
        "2026-11-20 10:00:00,7274,7290,7268,7285,18200\n"
    )
    imp = client.post("/api/candles/import-csv", json={"symbol": "SPX", "interval": "15m", "csv_text": csv_body})
    assert imp.status_code == 200
    assert imp.json()["barCount"] == 3

    ch = client.get("/api/candles?symbol=SPX&interval=15m")
    assert ch.status_code == 200
    cdata = ch.json()
    assert cdata["symbol"] == "SPX" and cdata["barCount"] == 3 and cdata["source"] == "cache"

    # 2. 创建复盘日记 + K 线打 Should have done 锚点 + 写入规则
    ce = client.post(
        "/api/entries",
        json={
            "symbol": "SPX",
            "interval": "15m",
            "title": "SPX 开盘回踩踩空复盘",
            "outcome": "missed",
            "bias_tags": ["怕回撤", "踏空犹豫"],
            "did_right_note": "盘前画清了支撑位，没有在开盘第一根追高。",
            "should_have_note": "09:45 回踩 EMA20 时信号很干净，但犹豫没进。",
            "takeaway_note": "信号确认且风险小于 0.5R 时直接按计划执行。",
        },
    )
    assert ce.status_code == 200
    assert "没有在开盘第一根追高" in ce.json()["entry"]["did_right_note"]
    eid = ce.json()["entry"]["id"]

    mk = client.post(
        f"/api/entries/{eid}/markers",
        json={"kind": "missed", "ts": cdata["candles"][1]["time"], "price": 7274.0, "note": "该进没进"},
    )
    assert mk.status_code == 200
    assert mk.json()["stats"]["markers"]["missed"] == 1

    rule = client.post("/api/rules", json={"entry_id": eid, "symbol": "SPX", "rule_text": "信号确认且风险<=0.5R直接进场"})
    assert rule.status_code == 200
    rid = rule.json()["rule"]["id"]
    hit = client.post(f"/api/rules/{rid}/hit")
    assert hit.status_code == 200 and hit.json()["rule"]["hit_count"] == 2

    # 3. 粘贴截图上传与静态读取
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKEPNG").decode()
    att = client.post(f"/api/entries/{eid}/attachments", json={"data_url": f"data:image/png;base64,{png_b64}"})
    assert att.status_code == 200
    img_url = att.json()["entry"]["attachments"][0]["url"]
    img_get = client.get(img_url)
    assert img_get.status_code == 200 and img_get.content.startswith(b"\x89PNG")


def test_volatility_radar_detects_extreme_day(tmp_path: Path, monkeypatch) -> None:
    from trade_journal import db, radar

    monkeypatch.setenv("TRADE_JOURNAL_DATA", str(tmp_path / "tj_radar"))
    conn = db.connect(tmp_path / "tj_radar" / "journal.db")
    db.init_db(conn)

    # 构造 30 天常规 ~1% 波动 + 最后一天暴涨 +12% 的极端大机会日
    bars = []
    px = 100.0
    for i in range(30):
        chg = 1.005 if i % 2 == 0 else 0.995
        if i == 29:
            chg = 1.12
        nxt = round(px * chg, 2)
        bars.append({"time": 1700000000 + i * 86400, "open": px, "high": max(px, nxt), "low": min(px, nxt), "close": nxt, "volume": 1000})
        px = nxt

    market.save_candles(conn, "NVDA", "1d", bars)
    market.save_candles(conn, "NDX", "1d", bars)
    rad = radar.scan_volatility_radar(conn, symbols=["NVDA", "NDX"], force=False)
    assert rad["regime"] == "extreme"
    assert rad["extremeCount"] == 2
    assert rad["items"][0]["peakSigma"] >= 5.0
    assert "大机会日" in rad["openingQuestion"]


def test_volatility_radar_detects_vix_compression_squeeze(tmp_path: Path, monkeypatch) -> None:
    from trade_journal import db, radar

    monkeypatch.setenv("TRADE_JOURNAL_DATA", str(tmp_path / "tj_sq"))
    conn = db.connect(tmp_path / "tj_sq" / "journal.db")
    db.init_db(conn)

    # 构造前 25 天 VIX 在 18–22 震荡，最近 5 天压到 12.8–13.1 冰点收窄
    vix_bars = []
    for i in range(30):
        px = 19.5 + (1.5 if i % 2 == 0 else -1.5) if i < 25 else 13.0 - (i - 25) * 0.05
        vix_bars.append({"time": 1700000000 + i * 86400, "open": px, "high": px + 0.4, "low": px - 0.3, "close": round(px, 2), "volume": 0})
    market.save_candles(conn, "VIX", "1d", vix_bars)

    rad = radar.scan_volatility_radar(conn, symbols=["VIX"], force=False)
    assert rad["regime"] == "squeeze"
    assert rad["vix"]["state"] == "compression"
    assert rad["vix"]["percentile60d"] <= 15.0
    assert "收窄" in rad["openingQuestion"]


def test_morning_desk_thermometer_and_daily_plan_signoff(tmp_path: Path, monkeypatch) -> None:
    from trade_journal import db, morning

    monkeypatch.setenv("TRADE_JOURNAL_DATA", str(tmp_path / "tj_morning"))
    db_file = tmp_path / "tj_morning" / "journal.db"
    conn = db.connect(db_file)
    db.init_db(conn)

    # 校验新增别名标准化
    assert market.normalize_symbol("海力士")[1] == "000660.KS"
    assert market.normalize_symbol("白银")[1] == "SI=F"
    assert market.normalize_symbol("WTI")[1] == "CL=F"
    assert market.normalize_symbol("CBRS")[0] == "CBRS"
    assert market.normalize_symbol("ANT")[0] == "ANT"
    assert market.normalize_symbol("DRAM")[0] == "DRAM"
    assert market.normalize_symbol("EUV")[0] == "EUV"

    # 构造 40 个交易日：前 39 天窄幅 ±0.5%，最后一天全部暴跌 -5.5%（验证 1 年罕见度 = 1 天）
    pool = ["NVDA", "AMD", "MU", "COHR", "AAPL", "TSLA"]
    for idx, sym in enumerate(pool):
        bars = []
        px = 100.0 + idx * 10
        for d in range(40):
            if d == 39:
                chg = 0.945 - idx * 0.002  # -5.5% ~ -6.5%
            else:
                chg = 1.004 if (d + idx) % 2 == 0 else 0.996
            nxt = round(px * chg, 2)
            bars.append({"time": 1760000000 + d * 86400, "open": px, "high": max(px, nxt), "low": min(px, nxt), "close": nxt, "volume": 1000})
            px = nxt
        market.save_candles(conn, sym, "1d", bars)
        market.save_candles(conn, sym, "1wk", bars[-10:])

    # VIX 高位 27.5
    vix_bars = [
        {"time": 1760000000 + d * 86400, "open": 16.0, "high": 16.5, "low": 15.5, "close": 16.0 if d < 39 else 27.5, "volume": 0}
        for d in range(40)
    ]
    market.save_candles(conn, "VIX", "1d", vix_bars)

    summary = morning.build_morning_summary(conn, force=False, allow_network=False)
    thermo = summary["thermometer"]
    assert thermo["totalCount"] == 6
    assert thermo["downCount"] == 6
    assert thermo["medianPct"] <= -5.5
    assert thermo["extremeDaysInYear"] == 1
    assert thermo["severity"] == "extreme"
    assert summary["vix"]["state"] == "expansion"

    charts = morning.build_morning_charts(conn, interval="1wk", force=False, allow_network=False)
    assert charts["interval"] == "1wk"
    assert len(charts["sections"]) == 5

    # 校验每日 30 秒计划签字 + 睡前闭环写入复盘日记
    client = TestClient(app_mod.create_app(db_file))
    sign_res = client.post(
        "/api/plans/today",
        json={"stance": "do_nothing", "plan_note": "全盘周线承压，今日空仓观望", "sign_morning": True},
    )
    assert sign_res.status_code == 200
    assert sign_res.json()["plan"]["is_signed"] is True
    assert sign_res.json()["streak"]["streak"] == 1

    ev_res = client.post(
        "/api/plans/today",
        json={
            "stance": "do_nothing",
            "plan_followed": "yes",
            "evening_review": "完美管住手没有乱接飞刀",
            "commit_evening": True,
        },
    )
    assert ev_res.status_code == 200
    assert ev_res.json()["plan"]["is_reviewed"] is True
    assert ev_res.json()["entry"] is not None
    assert "Do Nothing" in ev_res.json()["entry"]["title"]

    # 校验 3 步盘前计划 + 市场快照 + 月度 Daily Package 日历接口
    step_res = client.post(
        "/api/plans/today",
        json={
            "plan_date": "2026-11-26",
            "step1_note": "标普周线回踩 EMA50，大盘震荡偏弱",
            "step2_note": "SMH 领跌、XLV 防御走强",
            "step3_note": "今日 Do Nothing，等 NVDA 15m 企稳",
            "market_snapshot": {"vixPrice": 28.3, "medianPct": -1.18, "extremeDaysInYear": 65, "tradingDaysInYear": 252},
        },
    )
    assert step_res.status_code == 200
    assert step_res.json()["plan"]["has_plan"] is True
    assert "EMA50" in step_res.json()["plan"]["step1_note"]

    cal_res = client.get("/api/calendar?month=2026-11")
    assert cal_res.status_code == 200
    day26 = cal_res.json()["days"]["2026-11-26"]
    assert day26["hasPlan"] is True
    assert day26["marketSnapshot"]["vixPrice"] == 28.3

    pkg_res = client.get("/api/packages/2026-11-26")
    assert pkg_res.status_code == 200
    assert "Do Nothing" in pkg_res.json()["plan"]["step3_note"]


def test_normalize_dedupes_intraday_and_daily_live_ticks(tmp_path: Path, monkeypatch) -> None:
    from trade_journal import db

    monkeypatch.setenv("TRADE_JOURNAL_DATA", str(tmp_path / "tj_dedupe"))
    conn = db.connect(tmp_path / "tj_dedupe" / "journal.db")
    db.init_db(conn)

    # 模拟 GOLD 日线被盘中 30 秒自动刷新写入多个同一天不同秒级时间戳
    dup_daily = [
        {"time": 1795493716, "open": 3636.4, "high": 3676.7, "low": 3618.5, "close": 3631.2, "volume": 100},
        {"time": 1795494022, "open": 3636.4, "high": 3676.7, "low": 3618.5, "close": 3629.0, "volume": 120},
        {"time": 1795496400, "open": 3636.4, "high": 3676.7, "low": 3545.4, "close": 3561.9, "volume": 200},
    ]
    cnt = market.save_candles(conn, "GOLD", "1d", dup_daily)
    assert cnt == 1
    out = market.get_candles(conn, "GOLD", "1d", allow_network=False)
    assert out["barCount"] == 1
    assert out["lastPrice"] == 3561.9

    # 模拟 15m 盘中实时末根 12:45 + 12:52 合并为同一根 12:45 蜡烛
    dup_15m = [
        {"time": 1795524300, "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.5, "volume": 500},
        {"time": 1795524720, "open": 100.5, "high": 102.0, "low": 99.8, "close": 101.8, "volume": 750},
    ]
    cnt15 = market.save_candles(conn, "NVDA", "15m", dup_15m)
    assert cnt15 == 1
    out15 = market.get_candles(conn, "NVDA", "15m", allow_network=False)
    assert out15["candles"][0]["high"] == 102.0
    assert out15["candles"][0]["low"] == 99.5
    assert out15["candles"][0]["close"] == 101.8


def test_concurrent_sqlite_writes_are_thread_safe(tmp_path: Path, monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from trade_journal import db

    monkeypatch.setenv("TRADE_JOURNAL_DATA", str(tmp_path / "tj_conc"))
    conn = db.connect(tmp_path / "tj_conc" / "journal.db")
    db.init_db(conn)

    def writer_and_reader(idx: int) -> int:
        bars = [
            {"time": 1795000000 + d * 86400, "open": 100.0 + idx, "high": 105.0, "low": 98.0, "close": 102.0, "volume": 1000}
            for d in range(20)
        ]
        sym = f"SYM{idx % 6}"
        iv = "1d" if idx % 2 == 0 else "1wk"
        cnt = market.save_candles(conn, sym, iv, bars)
        got = market.get_candles(conn, sym, iv, allow_network=False)
        assert got["barCount"] == cnt
        assert all(isinstance(c["time"], int) for c in got["candles"])
        return cnt

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(writer_and_reader, range(32)))
    assert all(r == 20 if idx % 2 == 0 else r >= 3 for idx, r in enumerate(results))


def test_ai_generation_config_and_thought_filtering() -> None:
    from trade_journal import ai

    # Gemini 3+ / 4 uses thinkingLevel (never thinkingBudget)
    fast_38 = ai.build_generation_config("gemini-3.8-flash", tier="fast", response_mime_type="application/json")
    assert fast_38 == {
        "responseMimeType": "application/json",
        "thinkingConfig": {"thinkingLevel": "LOW"},
    }

    pro_4 = ai.build_generation_config("gemini-4-neon", tier="pro", pro_thinking="high")
    assert pro_4 == {"thinkingConfig": {"thinkingLevel": "HIGH"}}

    pro_4_xhigh = ai.build_generation_config("gemini-4-carbon", tier="pro", pro_thinking="xhigh")
    assert pro_4_xhigh == {"thinkingConfig": {"thinkingLevel": "XHIGH"}}

    # Legacy Gemini 2.5 uses thinkingBudget (0 for fast, dynamic/budget for pro)
    fast_25 = ai.build_generation_config("gemini-2.5-flash", tier="fast", response_mime_type="application/json")
    assert fast_25 == {
        "responseMimeType": "application/json",
        "thinkingConfig": {"thinkingBudget": 0},
    }
    pro_25_dyn = ai.build_generation_config("gemini-2.5-pro", tier="pro", pro_thinking="high")
    assert pro_25_dyn == {"thinkingConfig": {"thinkingBudget": -1}}
    pro_25_num = ai.build_generation_config("gemini-2.5-pro", tier="pro", pro_thinking="8192")
    assert pro_25_num == {"thinkingConfig": {"thinkingBudget": 8192}}

    # Thought parts must be filtered out so chain-of-thought never leaks into output
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thought": True, "text": "内部推理草稿不应输出"},
                        {"text": '{"title": "精准复盘"}'},
                    ]
                }
            }
        ]
    }
    assert ai.extract_text(payload) == '{"title": "精准复盘"}'


def test_ai_tiered_model_routing_across_three_endpoints(tmp_path: Path, monkeypatch) -> None:
    from trade_journal import ai, db

    monkeypatch.setenv("TRADE_JOURNAL_DATA", str(tmp_path / "tj_ai"))
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-123")
    monkeypatch.setenv("GEMINI_FAST_MODEL", "models/gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_PRO_MODEL", "gemini-4-neon")
    monkeypatch.setenv("GEMINI_PRO_THINKING", "high")

    db_file = tmp_path / "tj_ai" / "journal.db"
    conn = db.connect(db_file)
    db.init_db(conn)
    # 写入少量本地 K 线缓存避免 coach_reply 触发网络请求
    bars = [
        {"time": 1760000000 + d * 86400, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000}
        for d in range(10)
    ]
    market.save_candles(conn, "NVDA", "1d", bars)
    market.save_candles(conn, "VIX", "1d", bars)

    captured_calls: list[dict] = []

    class _FakeResp:
        def __init__(self, payload: dict) -> None:
            self.status_code = 200
            self.text = ""
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    def fake_post(url: str, *, params=None, headers=None, json=None, timeout=None):
        captured_calls.append({"url": url, "json": json, "timeout": timeout})
        if "gemini-3.8-flash" in url:
            body_text = (
                '{"title": "NVDA 突破踏空复盘", "outcome": "missed", "primary_symbol": "NVDA", '
                '"symbols": ["NVDA"], "bias_tags": ["踏空犹豫"], "context_note": "放量突破", '
                '"execution_note": "没上车", "did_right_note": "没乱追", '
                '"should_have_note": "0.5R 试仓 Call", "takeaway_note": "锁死最大亏损直接上车", "needs_chart": "single"}'
            )
        else:
            body_text = "1. 核心漏洞：高赔率窗口不敢下注。\n2. 拍板 3 问：① NVDA ② 3DTE Call ③ 最大亏 $500。"
        return _FakeResp(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "内部思考过程"},
                                {"text": body_text},
                            ]
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(ai.httpx, "post", fake_post)
    monkeypatch.setattr(
        app_mod.morning,
        "build_morning_summary",
        lambda conn, force=False, allow_network=True: {
            "regime": "extreme",
            "regimeLabel": "极端大波动",
            "freshness": {"label": "本地缓存"},
            "vix": {"price": 28.5, "percentile60d": 92.0, "percentile1y": 88.0, "dayPct": 12.0, "weekPct": 25.0, "squeezeRatio": 1.4, "stateLabel": "恐慌放大"},
            "thermometer": {"totalCount": 1, "latestDate": "2026-11-27", "medianPct": -4.2, "meanPct": -4.2, "upCount": 0, "downCount": 1, "rarityHeadline": "1年罕见", "groupStats": []},
            "plan": {"step1_note": "大盘承压", "step2_note": "SMH 弱", "step3_note": "等止跌"},
        },
    )

    client = TestClient(app_mod.create_app(db_file))

    # 0. Bootstrap exposes active fast/pro models
    boot = client.get("/api/bootstrap")
    assert boot.status_code == 200
    assert boot.json()["aiModels"] == {
        "provider": "api",
        "fast": "gemini-3.8-flash",
        "pro": "gemini-4-neon",
        "proThinking": "high",
    }

    # 1. Organize uses FAST model + low thinking + JSON mime type
    ce = client.post("/api/entries", json={"symbol": "NVDA", "raw_note": "今天 NVDA 放量突破没敢买 Call"})
    eid = ce.json()["entry"]["id"]
    org = client.post(f"/api/entries/{eid}/organize", json={})
    assert org.status_code == 200
    assert org.json()["model"] == "gemini-3.8-flash"
    assert org.json()["entry"]["title"] == "NVDA 突破踏空复盘"
    assert "gemini-3.8-flash:generateContent" in captured_calls[0]["url"]
    assert captured_calls[0]["json"]["generationConfig"] == {
        "responseMimeType": "application/json",
        "thinkingConfig": {"thinkingLevel": "LOW"},
    }
    assert captured_calls[0]["timeout"] == ai.FAST_TIMEOUT_SEC

    # 2. Volatility Coach uses PRO model + HIGH thinking + longer timeout
    coach = client.post("/api/coach/reply", json={"message": "VIX 飙到 28 了，我在犹豫要不要抄底 NVDA"})
    assert coach.status_code == 200
    assert coach.json()["model"] == "gemini-4-neon"
    assert "内部思考过程" not in coach.json()["reply"]
    assert "gemini-4-neon:generateContent" in captured_calls[1]["url"]
    assert captured_calls[1]["json"]["generationConfig"] == {
        "thinkingConfig": {"thinkingLevel": "HIGH"},
    }
    assert captured_calls[1]["timeout"] == ai.PRO_TIMEOUT_SEC

    # 3. Behavior Mode Review uses PRO model + HIGH thinking + longer timeout
    rev = client.post("/api/ai/review", json={"focus": "踏空问题"})
    assert rev.status_code == 200
    assert rev.json()["model"] == "gemini-4-neon"
    assert "内部思考过程" not in rev.json()["reply"]
    assert "gemini-4-neon:generateContent" in captured_calls[2]["url"]
    assert captured_calls[2]["json"]["generationConfig"] == {
        "thinkingConfig": {"thinkingLevel": "HIGH"},
    }
    assert captured_calls[2]["timeout"] == ai.PRO_TIMEOUT_SEC


def test_ai_provider_auto_resolution_and_agy_cli_headless(monkeypatch) -> None:
    import subprocess
    from trade_journal import ai

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_PROVIDER", "auto")
    monkeypatch.setattr(ai, "find_agy_bin", lambda: "/fake/bin/agy")
    monkeypatch.setattr(ai, "find_gemini_cli_bin", lambda: "/fake/bin/gemini")
    assert ai.resolve_provider() == "agy"

    seen_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen_cmds.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout='{"status":"SUCCESS","response":"推演完成：锁死 0.5R 上限。"}',
            stderr="",
        )

    monkeypatch.setattr(ai.subprocess, "run", fake_run)
    text_fast, m_fast = ai.generate_content("整理口述", tier="fast")
    assert text_fast == "推演完成：锁死 0.5R 上限。"
    assert m_fast == f"agy:{ai.DEFAULT_AGY_FAST_MODEL}"
    assert "--model" in seen_cmds[0] and seen_cmds[0][seen_cmds[0].index("--model") + 1] == ai.DEFAULT_AGY_FAST_MODEL
    assert "--disable-slash-commands" in seen_cmds[0]

    monkeypatch.setenv("AGY_PRO_MODEL", "gemini-4-carbon-high")
    text_pro, m_pro = ai.generate_content("深度复盘", tier="pro")
    assert text_pro == "推演完成：锁死 0.5R 上限。"
    assert m_pro == "agy:gemini-4-carbon-high"
    assert "--model" in seen_cmds[1] and seen_cmds[1][seen_cmds[1].index("--model") + 1] == "gemini-4-carbon-high"


def test_ai_gemini_cli_headless_and_error_hint(monkeypatch) -> None:
    import subprocess
    from trade_journal import ai

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_PROVIDER", "auto")
    monkeypatch.setattr(ai, "find_agy_bin", lambda: None)
    monkeypatch.setattr(ai, "find_gemini_cli_bin", lambda: "/fake/bin/gemini")
    assert ai.resolve_provider() == "gemini-cli"

    def fake_run_ok(cmd, **kwargs):
        assert "--skip-trust" in cmd
        assert "-o" in cmd and cmd[cmd.index("-o") + 1] == "json"
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout='{"response":"行为漏洞：高频踏空后追高。"}',
            stderr="Loaded cached credentials.\n",
        )

    monkeypatch.setattr(ai.subprocess, "run", fake_run_ok)
    txt, model = ai.generate_content("审计复盘", tier="pro")
    assert txt == "行为漏洞：高频踏空后追高。"
    assert model == f"gemini-cli:{ai.DEFAULT_GEMINI_CLI_PRO_MODEL}"

    # Verify helpful error message when personal OAuth is rejected by Gemini CLI
    def fake_run_err(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="An unexpected critical error occurred: This client is no longer supported for Gemini Code Assist for individuals.",
        )

    monkeypatch.setattr(ai.subprocess, "run", fake_run_err)
    try:
        ai.generate_content("审计复盘", tier="pro")
        assert False, "expected GeminiError"
    except ai.GeminiError as exc:
        assert exc.status_code == 502
        assert "Antigravity CLI" in str(exc)







