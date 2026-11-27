"""Trade Journal FastAPI 应用：本地 K 线接口、Slash 命令解析、交易复盘 CRUD、K 线打点、截图粘贴与 Gemini 复盘汇总。"""
from __future__ import annotations

import base64
import json
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, db, market, morning, radar

STATIC_DIR = Path(__file__).parent / "static"


class DailyPlanUpsertReq(BaseModel):
    plan_date: str = ""
    stance: str | None = None  # do_nothing | watch | bet
    plan_note: str | None = None
    step1_note: str | None = None
    step2_note: str | None = None
    step3_note: str | None = None
    market_snapshot: dict[str, Any] | None = None
    focus_symbols: list[str] | None = None
    plan_followed: str | None = None  # yes | partial | no
    evening_review: str | None = None
    sign_morning: bool = False
    commit_evening: bool = False


class CoachReplyReq(BaseModel):
    message: str


class CommandReq(BaseModel):
    command: str


class EntryCreateReq(BaseModel):
    trade_date: str = ""
    title: str = ""
    symbol: str = "SPX"
    interval: str = "15m"
    outcome: str = "review"
    pnl: str = ""
    bias_tags: list[str] = []
    raw_note: str = ""
    context_note: str = ""
    symbols: list[str] = []
    execution_note: str = ""
    did_right_note: str = ""
    should_have_note: str = ""
    takeaway_note: str = ""


class EntryPatchReq(BaseModel):
    trade_date: str | None = None
    title: str | None = None
    symbol: str | None = None
    interval: str | None = None
    outcome: str | None = None
    pnl: str | None = None
    bias_tags: list[str] | None = None
    raw_note: str | None = None
    context_note: str | None = None
    symbols: list[str] | None = None
    execution_note: str | None = None
    did_right_note: str | None = None
    should_have_note: str | None = None
    takeaway_note: str | None = None


class SnapshotReq(BaseModel):
    symbols: list[str] = ["NDX", "SPX", "NVDA", "BTC", "GOLD"]
    days: int = 14
    force: bool = False


class OrganizeReq(BaseModel):
    raw_note: str | None = None


class MarkerCreateReq(BaseModel):
    kind: str  # buy | sell | missed | mistake
    ts: int
    price: float
    label: str = ""
    note: str = ""


class MarkerPatchReq(BaseModel):
    kind: str | None = None
    price: float | None = None
    label: str | None = None
    note: str | None = None


class RuleCreateReq(BaseModel):
    entry_id: int | None = None
    symbol: str = ""
    rule_text: str
    category: str = "执行纪律"


class AttachmentUploadReq(BaseModel):
    data_url: str
    caption: str = ""


class CsvImportReq(BaseModel):
    symbol: str
    interval: str = "15m"
    csv_text: str


class AIReviewReq(BaseModel):
    focus: str = ""


def _build_stats(conn) -> dict[str, Any]:
    entries = conn.execute("SELECT outcome, bias_tags_json FROM entries").fetchall()
    markers = conn.execute("SELECT kind FROM markers").fetchall()
    bias_counter: Counter[str] = Counter()
    outcome_counter: Counter[str] = Counter()
    for e in entries:
        outcome_counter[e["outcome"]] += 1
        try:
            for t in json.loads(e["bias_tags_json"] or "[]"):
                if str(t).strip():
                    bias_counter[str(t).strip()] += 1
        except ValueError:
            pass
    mk_counter = Counter(m["kind"] for m in markers)
    return {
        "totalEntries": len(entries),
        "outcomes": dict(outcome_counter),
        "markers": {
            "buy": mk_counter.get("buy", 0),
            "sell": mk_counter.get("sell", 0),
            "missed": mk_counter.get("missed", 0),
            "mistake": mk_counter.get("mistake", 0),
        },
        "topBiases": [{"tag": k, "count": v} for k, v in bias_counter.most_common(10)],
    }


def create_app(db_path: Path | None = None) -> FastAPI:
    conn = db.connect(db_path)
    db.init_db(conn)

    app = FastAPI(title="Trade Journal", version="0.1.0")

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        today = db.today_str()
        entry_rows = conn.execute("SELECT * FROM entries ORDER BY trade_date DESC, id DESC").fetchall()
        rule_rows = conn.execute("SELECT * FROM rules ORDER BY hit_count DESC, id DESC").fetchall()
        plan_row = conn.execute("SELECT * FROM daily_plans WHERE plan_date=?", (today,)).fetchone()
        return {
            "today": today,
            "entries": [db.entry_to_dict(conn, r) for r in entry_rows],
            "rules": [dict(r) for r in rule_rows],
            "cache": market.list_cache_summary(conn),
            "quickSymbols": market.quick_symbols(),
            "intervals": list(market.VALID_INTERVALS),
            "stats": _build_stats(conn),
            "todayPlan": db.plan_to_dict(plan_row, today),
            "planStreak": db.daily_plan_streak(conn),
            "hasAi": ai.is_configured(),
            "hasGeminiKey": bool(ai.get_api_key()),
            "hasAgyCli": bool(ai.find_agy_bin()),
            "hasGeminiCli": bool(ai.find_gemini_cli_bin()),
            "aiProvider": ai.resolve_provider(),
            "aiModels": ai.get_model_config(),
        }

    @app.get("/api/candles")
    def api_candles(symbol: str = "SPX", interval: str = "15m", force: bool = False) -> dict[str, Any]:
        try:
            return market.get_candles(conn, symbol, interval, force=force)
        except market.MarketError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/command")
    def api_command(req: CommandReq) -> dict[str, Any]:
        parsed = market.parse_slash_command(req.command)
        if parsed["action"] == "tv":
            return {"ok": True, "action": "tv", "hint": "可直接按 Cmd+V 粘贴 TradingView 截图到右侧复盘卡"}
        try:
            data = market.get_candles(conn, parsed["symbol"], parsed["interval"], force=parsed["force"])
        except market.MarketError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "action": "chart", "parsed": parsed, "chart": data}

    @app.post("/api/market/snapshot")
    def api_market_snapshot(req: SnapshotReq) -> dict[str, Any]:
        return market.get_multi_asset_snapshot(conn, req.symbols, days=req.days, force=req.force)

    @app.post("/api/candles/import-csv")
    def api_import_csv(req: CsvImportReq) -> dict[str, Any]:
        try:
            count = market.import_csv_ohlcv(conn, req.symbol, req.interval, req.csv_text)
        except market.MarketError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "barCount": count, "cache": market.list_cache_summary(conn)}

    @app.get("/api/entries")
    def list_entries(q: str = "", symbol: str = "") -> dict[str, Any]:
        sql = "SELECT * FROM entries WHERE 1=1"
        params: list[Any] = []
        if symbol.strip():
            sym, _, _ = market.normalize_symbol(symbol)
            sql += " AND UPPER(symbol)=?"
            params.append(sym)
        if q.strip():
            like = f"%{q.strip()}%"
            sql += " AND (title LIKE ? OR symbol LIKE ? OR raw_note LIKE ? OR context_note LIKE ? OR execution_note LIKE ? OR did_right_note LIKE ? OR should_have_note LIKE ? OR takeaway_note LIKE ?)"
            params.extend([like, like, like, like, like, like, like, like])
        sql += " ORDER BY trade_date DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
        return {"entries": [db.entry_to_dict(conn, r) for r in rows]}

    @app.post("/api/entries")
    def create_entry(req: EntryCreateReq) -> dict[str, Any]:
        sym, _, label = market.normalize_symbol(req.symbol)
        iv = req.interval if req.interval in market.VALID_INTERVALS else "15m"
        tdate = req.trade_date.strip() or db.today_str()
        title = req.title.strip() or f"{tdate} {sym} ({iv}) 交易复盘"
        ts = db.now_iso()
        cur = conn.execute(
            """
            INSERT INTO entries(
                trade_date, title, symbol, interval, outcome, pnl,
                bias_tags_json, raw_note, context_note, symbols_json,
                execution_note, did_right_note, should_have_note, takeaway_note,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tdate,
                title,
                sym,
                iv,
                req.outcome,
                req.pnl.strip(),
                json.dumps(req.bias_tags, ensure_ascii=False),
                req.raw_note,
                req.context_note,
                json.dumps(req.symbols, ensure_ascii=False),
                req.execution_note,
                req.did_right_note,
                req.should_have_note,
                req.takeaway_note,
                ts,
                ts,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM entries WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"ok": True, "entry": db.entry_to_dict(conn, row), "stats": _build_stats(conn)}

    @app.patch("/api/entries/{entry_id}")
    def patch_entry(entry_id: int, req: EntryPatchReq) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, "复盘记录不存在")
        fields: dict[str, Any] = {}
        if req.trade_date is not None:
            fields["trade_date"] = req.trade_date.strip() or row["trade_date"]
        if req.title is not None:
            fields["title"] = req.title.strip() or row["title"]
        if req.symbol is not None:
            fields["symbol"] = market.normalize_symbol(req.symbol)[0]
        if req.interval is not None and req.interval in market.VALID_INTERVALS:
            fields["interval"] = req.interval
        if req.outcome is not None:
            fields["outcome"] = req.outcome
        if req.pnl is not None:
            fields["pnl"] = req.pnl.strip()
        if req.bias_tags is not None:
            fields["bias_tags_json"] = json.dumps(req.bias_tags, ensure_ascii=False)
        if req.raw_note is not None:
            fields["raw_note"] = req.raw_note
        if req.context_note is not None:
            fields["context_note"] = req.context_note
        if req.symbols is not None:
            fields["symbols_json"] = json.dumps(req.symbols, ensure_ascii=False)
        if req.execution_note is not None:
            fields["execution_note"] = req.execution_note
        if req.did_right_note is not None:
            fields["did_right_note"] = req.did_right_note
        if req.should_have_note is not None:
            fields["should_have_note"] = req.should_have_note
        if req.takeaway_note is not None:
            fields["takeaway_note"] = req.takeaway_note

        if fields:
            fields["updated_at"] = db.now_iso()
            set_sql = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE entries SET {set_sql} WHERE id=?", [*fields.values(), entry_id])
            conn.commit()

        fresh = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return {"ok": True, "entry": db.entry_to_dict(conn, fresh), "stats": _build_stats(conn)}

    @app.post("/api/entries/{entry_id}/organize")
    def organize_entry(entry_id: int, req: OrganizeReq | None = None) -> dict[str, Any]:
        """将自由口述/随手写的交易复盘，用 Gemini 一键归类成：背景逻辑、实际执行/赔率测算、Should have done、Takeaway 铁律、偏差标签与关联标的。"""
        row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, "复盘记录不存在")
        raw_text = (req.raw_note if (req and req.raw_note is not None) else row["raw_note"]) or ""
        if not raw_text.strip():
            raise HTTPException(400, "请先在口述/自由记录框里写下或粘贴你的复盘内容")

        prompt = (
            "你是一位顶级美股与加密货币交易复盘助理。交易员刚刚口述了一段原始交易复盘（可能含中英混说、ASR 同音错别字如“靠”=Call、“迈贝尔”=Marvell、“拉姆研究”=Lam Research）。\n"
            "请在不丢失任何关键价格、赔率数字和原始洞察的前提下，将其归类整理为严格 JSON 对象，只返回 JSON，不要 markdown 以外的多余文字：\n"
            "{\n"
            '  "title": "不超过22字的精准复盘标题",\n'
            '  "outcome": "win | loss | missed | review（四选一）",\n'
            '  "primary_symbol": "最核心标的标准化代号（如 NVDA、SPX、NDX、QQQ、BTC、GOLD）",\n'
            '  "symbols": ["提到的所有可画图核心标的代号，最多6个，如 NDX, SPX, NVDA, BTC, GOLD"],\n'
            '  "bias_tags": ["2-4个简短中文偏差/行为标签，如 踏空犹豫, 不敢下注, 怕回撤, 低估反转幅度"],\n'
            '  "context_note": "用简洁连贯的段落梳理宏观/事件背景、超跌位置与核心判断逻辑（保留关键价位与事件）",\n'
            '  "execution_note": "梳理实际操作或观察到的关键行情/期权赔率测算（如具体行权价、入场价、倍数，保留精准数字）",\n'
            '  "did_right_note": "客观提炼这笔观察或交易里做对了什么（如看懂了逻辑、守住了风控、识别了极端波动、没有乱追高等，没有可留空字符串）",\n'
            '  "should_have_note": "提炼 I should have done, but I didn\'t（具体该怎么下注/策略、为什么没做）",\n'
            '  "takeaway_note": "提炼 1-2 句极简可执行的下次铁律（保留交易员最锐利的原话风格）",\n'
            '  "needs_chart": "snapshot（涉及多资产大波动对比） | single（单一标的打点） | none（纯文字复盘无需图）"\n'
            "}\n\n"
            f"交易员原始口述：\n{raw_text}"
        )
        try:
            txt, used_model = ai.generate_content(prompt, tier="fast", response_mime_type="application/json")
        except ai.GeminiError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", txt)
        cand = m.group(1).strip() if m else txt
        i, j = cand.find("{"), cand.rfind("}")
        if i == -1 or j <= i:
            raise HTTPException(502, "Gemini 返回格式无法解析为 JSON，请重试")
        try:
            parsed = json.loads(cand[i : j + 1])
        except ValueError as exc:
            raise HTTPException(502, f"JSON 解析失败：{exc}") from exc

        primary_sym = market.normalize_symbol(str(parsed.get("primary_symbol") or row["symbol"]))[0]
        raw_syms = parsed.get("symbols") if isinstance(parsed.get("symbols"), list) else [primary_sym]
        norm_syms: list[str] = []
        for s in raw_syms:
            ns = market.normalize_symbol(str(s))[0]
            if ns and ns not in norm_syms:
                norm_syms.append(ns)
        if primary_sym not in norm_syms:
            norm_syms.insert(0, primary_sym)

        outcome = str(parsed.get("outcome") or "review")
        if outcome not in ("win", "loss", "missed", "review"):
            outcome = "review"
        bias_tags = [str(x).strip() for x in (parsed.get("bias_tags") or []) if str(x).strip()][:6]

        conn.execute(
            """
            UPDATE entries SET
                title=?, symbol=?, outcome=?, bias_tags_json=?, raw_note=?,
                context_note=?, symbols_json=?, execution_note=?, did_right_note=?, should_have_note=?, takeaway_note=?,
                updated_at=?
            WHERE id=?
            """,
            (
                str(parsed.get("title") or row["title"])[:60],
                primary_sym,
                outcome,
                json.dumps(bias_tags, ensure_ascii=False),
                raw_text,
                str(parsed.get("context_note") or ""),
                json.dumps(norm_syms[:6], ensure_ascii=False),
                str(parsed.get("execution_note") or ""),
                str(parsed.get("did_right_note") or ""),
                str(parsed.get("should_have_note") or ""),
                str(parsed.get("takeaway_note") or ""),
                db.now_iso(),
                entry_id,
            ),
        )
        conn.commit()
        fresh = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        entry_dict = db.entry_to_dict(conn, fresh)

        needs_chart = str(parsed.get("needs_chart") or ("snapshot" if len(norm_syms) >= 2 else "single"))
        snapshot = market.get_multi_asset_snapshot(conn, norm_syms, days=14) if len(norm_syms) >= 2 else None
        return {
            "ok": True,
            "entry": entry_dict,
            "needsChart": needs_chart,
            "snapshot": snapshot,
            "stats": _build_stats(conn),
            "model": used_model,
        }

    @app.delete("/api/entries/{entry_id}")
    def delete_entry(entry_id: int) -> dict[str, Any]:
        conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        conn.commit()
        return {"ok": True, "stats": _build_stats(conn)}

    @app.post("/api/entries/{entry_id}/markers")
    def add_marker(entry_id: int, req: MarkerCreateReq) -> dict[str, Any]:
        if not conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone():
            raise HTTPException(404, "复盘记录不存在")
        kind = req.kind if req.kind in ("buy", "sell", "missed", "mistake") else "missed"
        default_labels = {
            "buy": "实际买入 / 做多",
            "sell": "实际卖出 / 止损",
            "missed": "Should have done",
            "mistake": "Shouldn't have done",
        }
        cur = conn.execute(
            "INSERT INTO markers(entry_id, kind, ts, price, label, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry_id, kind, int(req.ts), float(req.price), req.label.strip() or default_labels[kind], req.note.strip(), db.now_iso()),
        )
        conn.execute("UPDATE entries SET updated_at=? WHERE id=?", (db.now_iso(), entry_id))
        conn.commit()
        row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return {"ok": True, "markerId": cur.lastrowid, "entry": db.entry_to_dict(conn, row), "stats": _build_stats(conn)}

    @app.patch("/api/markers/{marker_id}")
    def patch_marker(marker_id: int, req: MarkerPatchReq) -> dict[str, Any]:
        m = conn.execute("SELECT * FROM markers WHERE id=?", (marker_id,)).fetchone()
        if not m:
            raise HTTPException(404, "锚点不存在")
        fields: dict[str, Any] = {}
        if req.kind in ("buy", "sell", "missed", "mistake"):
            fields["kind"] = req.kind
        if req.price is not None:
            fields["price"] = float(req.price)
        if req.label is not None:
            fields["label"] = req.label.strip()
        if req.note is not None:
            fields["note"] = req.note.strip()
        if fields:
            set_sql = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE markers SET {set_sql} WHERE id=?", [*fields.values(), marker_id])
            conn.commit()
        row = conn.execute("SELECT * FROM entries WHERE id=?", (m["entry_id"],)).fetchone()
        return {"ok": True, "entry": db.entry_to_dict(conn, row), "stats": _build_stats(conn)}

    @app.delete("/api/markers/{marker_id}")
    def delete_marker(marker_id: int) -> dict[str, Any]:
        m = conn.execute("SELECT entry_id FROM markers WHERE id=?", (marker_id,)).fetchone()
        if not m:
            raise HTTPException(404, "锚点不存在")
        eid = m["entry_id"]
        conn.execute("DELETE FROM markers WHERE id=?", (marker_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
        return {"ok": True, "entry": db.entry_to_dict(conn, row), "stats": _build_stats(conn)}

    @app.post("/api/entries/{entry_id}/attachments")
    def upload_attachment(entry_id: int, req: AttachmentUploadReq) -> dict[str, Any]:
        if not conn.execute("SELECT 1 FROM entries WHERE id=?", (entry_id,)).fetchone():
            raise HTTPException(404, "复盘记录不存在")
        m = re.match(r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$", req.data_url.strip(), re.S)
        if not m:
            raise HTTPException(400, "请粘贴或上传 PNG / JPG / WebP 图片")
        ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
        raw = base64.b64decode(m.group(2))
        if len(raw) > 12 * 1024 * 1024:
            raise HTTPException(400, "单张截图不能超过 12MB")
        fname = f"tv_{entry_id}_{uuid.uuid4().hex[:8]}.{ext}"
        (db.media_dir() / fname).write_bytes(raw)
        conn.execute(
            "INSERT INTO attachments(entry_id, filename, caption, created_at) VALUES (?, ?, ?, ?)",
            (entry_id, fname, req.caption.strip() or "TradingView 截图", db.now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return {"ok": True, "entry": db.entry_to_dict(conn, row)}

    @app.delete("/api/attachments/{att_id}")
    def delete_attachment(att_id: int) -> dict[str, Any]:
        r = conn.execute("SELECT entry_id, filename FROM attachments WHERE id=?", (att_id,)).fetchone()
        if not r:
            raise HTTPException(404, "截图不存在")
        (db.media_dir() / r["filename"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM attachments WHERE id=?", (att_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM entries WHERE id=?", (r["entry_id"],)).fetchone()
        return {"ok": True, "entry": db.entry_to_dict(conn, row)}

    @app.get("/api/media/{filename}")
    def get_media(filename: str) -> FileResponse:
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "非法文件名")
        fp = db.media_dir() / filename
        if not fp.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(fp)

    @app.post("/api/rules")
    def create_rule(req: RuleCreateReq) -> dict[str, Any]:
        text = req.rule_text.strip()
        if not text:
            raise HTTPException(400, "规则内容不能为空")
        cur = conn.execute(
            "INSERT INTO rules(entry_id, symbol, rule_text, category, hit_count, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (req.entry_id, req.symbol.strip().upper(), text, req.category.strip() or "执行纪律", db.now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM rules WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"ok": True, "rule": dict(row)}

    @app.post("/api/rules/{rule_id}/hit")
    def hit_rule(rule_id: int) -> dict[str, Any]:
        conn.execute("UPDATE rules SET hit_count = hit_count + 1 WHERE id=?", (rule_id,))
        conn.commit()
        row = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        if not row:
            raise HTTPException(404, "规则不存在")
        return {"ok": True, "rule": dict(row)}

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: int) -> dict[str, Any]:
        conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        conn.commit()
        return {"ok": True}

    @app.post("/api/ai/review")
    def ai_review(req: AIReviewReq | None = None) -> dict[str, Any]:
        if not ai.is_configured():
            raise HTTPException(
                400,
                "未检测到可用的 AI 后端：请配置 GOOGLE_API_KEY / GEMINI_API_KEY，或安装并登录 Antigravity CLI (`agy`) / Gemini CLI",
            )
        rows = conn.execute("SELECT * FROM entries ORDER BY trade_date DESC, id DESC LIMIT 20").fetchall()
        if not rows:
            raise HTTPException(400, "还没有交易复盘记录，先写 1 条再来让 Gemini 总结")
        entries = [db.entry_to_dict(conn, r) for r in rows]
        digest_lines = []
        for e in entries:
            mk_txt = "；".join(f"{m['kind']}@{m['price']}({m['note'] or m['label']})" for m in e["markers"])
            digest_lines.append(
                f"- [{e['trade_date']}] {e['symbol']}({e['interval']}) 结论={e['outcome']} PnL={e['pnl'] or '无'} 标签={','.join(e['bias_tags'])}\n"
                f"  锚点：{mk_txt or '无'}\n"
                f"  实际执行：{e['execution_note'][:220]}\n"
                f"  做对了什么：{e['did_right_note'][:220]}\n"
                f"  Should have done：{e['should_have_note'][:220]}\n"
                f"  下次学到：{e['takeaway_note'][:220]}"
            )
        prompt = (
            "你是一位冷峻、一针见血的机构交易心理与执行教练。下面是交易员近期真实的交易复盘日记：\n\n"
            + "\n".join(digest_lines)
            + (f"\n\n用户额外关注问题：{req.focus}\n" if (req and req.focus.strip()) else "")
            + "\n\n请用简洁中文输出 3 个模块：\n"
            "1. **重复最贵的行为漏洞**（重点看 Should have done 和破纪律的共性模式）\n"
            "2. **下周最值钱的 3 条刚性执行铁律**（可直接贴在屏幕边）\n"
            "3. **一句客观复盘点评**（不鸡汤、只讲概率与执行）"
        )
        try:
            text, used_model = ai.generate_content(prompt, tier="pro")
        except ai.GeminiError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        if not text:
            raise HTTPException(502, "行为模式复盘返回为空")
        return {"ok": True, "reply": text, "model": used_model}

    # ---------- Proactive Volatility Radar & Bet-Push Coach ----------
    def _coach_messages(day: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT id, session_day, role, content, regime, created_at FROM coach_messages WHERE session_day=? ORDER BY id ASC",
            (day,),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/coach/state")
    def coach_state(force: bool = False) -> dict[str, Any]:
        summary = morning.build_morning_summary(conn, force=force)
        day = db.today_str()
        msgs = _coach_messages(day)
        opening = morning.build_coach_opening(summary)
        # 如果今天还没有对话，或者只有系统自动生成的首条旧开场白且本次是强刷/口径更新，同步为最新带时效戳的开场白
        if not msgs:
            conn.execute(
                "INSERT INTO coach_messages(session_day, role, content, regime, created_at) VALUES (?, 'agent', ?, ?, ?)",
                (day, opening, summary["regime"], db.now_iso()),
            )
            conn.commit()
            msgs = _coach_messages(day)
        elif len(msgs) == 1 and msgs[0]["role"] == "agent" and (force or "🕒 时效：" not in msgs[0]["content"]):
            conn.execute(
                "UPDATE coach_messages SET content=?, regime=?, created_at=? WHERE id=?",
                (opening, summary["regime"], db.now_iso(), msgs[0]["id"]),
            )
            conn.commit()
            msgs = _coach_messages(day)
        return {
            "ok": True,
            "summary": summary,
            "regime": summary["regime"],
            "regimeLabel": summary["regimeLabel"],
            "freshness": summary["freshness"],
            "messages": msgs,
        }

    @app.post("/api/coach/reset")
    def coach_reset() -> dict[str, Any]:
        day = db.today_str()
        conn.execute("DELETE FROM coach_messages WHERE session_day=?", (day,))
        summary = morning.build_morning_summary(conn, force=True)
        opening = morning.build_coach_opening(summary)
        conn.execute(
            "INSERT INTO coach_messages(session_day, role, content, regime, created_at) VALUES (?, 'agent', ?, ?, ?)",
            (day, opening, summary["regime"], db.now_iso()),
        )
        conn.commit()
        return {
            "ok": True,
            "summary": summary,
            "regime": summary["regime"],
            "regimeLabel": summary["regimeLabel"],
            "freshness": summary["freshness"],
            "messages": _coach_messages(day),
        }

    @app.post("/api/coach/reply")
    def coach_reply(req: CoachReplyReq) -> dict[str, Any]:
        user_msg = (req.message or "").strip()
        if not user_msg:
            raise HTTPException(400, "请输入你的回复")
        if not ai.is_configured():
            raise HTTPException(
                400,
                "未检测到可用的 AI 后端：请配置 GOOGLE_API_KEY / GEMINI_API_KEY，或安装并登录 Antigravity CLI (`agy`) / Gemini CLI",
            )

        day = db.today_str()
        summary = morning.build_morning_summary(conn, force=False)
        if not _coach_messages(day):
            conn.execute(
                "INSERT INTO coach_messages(session_day, role, content, regime, created_at) VALUES (?, 'agent', ?, ?, ?)",
                (day, morning.build_coach_opening(summary), summary["regime"], db.now_iso()),
            )
        conn.execute(
            "INSERT INTO coach_messages(session_day, role, content, regime, created_at) VALUES (?, 'user', ?, ?, ?)",
            (day, user_msg, summary["regime"], db.now_iso()),
        )
        conn.commit()

        history = _coach_messages(day)[-12:]
        rules_rows = conn.execute("SELECT rule_text, hit_count FROM rules ORDER BY hit_count DESC, id DESC LIMIT 8").fetchall()
        rules_block = "\n".join(f"- {r['rule_text']}（已提醒 {r['hit_count']} 次）" for r in rules_rows) or "- 当真的有极端波动的时候，一定要 sit down and think about it and make a bet，不要不敢下注。"

        regret_rows = conn.execute(
            "SELECT trade_date, symbol, should_have_note, takeaway_note FROM entries WHERE should_have_note != '' ORDER BY id DESC LIMIT 4"
        ).fetchall()
        regret_block = "\n".join(
            f"- [{r['trade_date']} {r['symbol']}] 踏空教训：{r['should_have_note'][:140]} | 铁律：{r['takeaway_note'][:120]}"
            for r in regret_rows
        ) or "暂无历史踏空记录"

        vix = summary.get("vix")
        vix_block = (
            f"VIX 现价={vix['price']} | 60日分位={vix['percentile60d']}% · 1年分位={vix['percentile1y']}% | 今日={vix['dayPct']:+.2f}% | 5日={vix['weekPct']:+.2f}% | 压缩比={vix['squeezeRatio']}× | 状态={vix['stateLabel']}"
            if vix
            else "暂无 VIX 数据"
        )
        t = summary.get("thermometer") or {}
        group_txt = "；".join(
            f"{g['title']}中位数 {g['medianPct']:+.2f}%({g['upCount']}涨/{g['downCount']}跌)"
            for g in (t.get("groupStats") or [])
        )
        thermo_block = (
            f"数据时效：{summary['freshness']['label']}\n"
            f"自选 {t.get('totalCount', 0)} 只股票（{t.get('latestDate', '--')}）：中位数={t.get('medianPct', 0):+.2f}% | 平均={t.get('meanPct', 0):+.2f}% | {t.get('upCount', 0)}涨/{t.get('downCount', 0)}跌\n"
            f"1年罕见度：{t.get('rarityHeadline', '')}\n"
            f"分组表现：{group_txt or '无'}"
        )
        plan = summary.get("plan") or {}
        plan_parts = [
            f"Step 1 大盘：{plan.get('step1_note')}" if plan.get("step1_note") else "",
            f"Step 2 板块：{plan.get('step2_note')}" if plan.get("step2_note") else "",
            f"Step 3 决策：{plan.get('step3_note') or plan.get('plan_note')}" if (plan.get("step3_note") or plan.get("plan_note")) else "",
        ]
        plan_block = "\n".join(x for x in plan_parts if x) or "交易员今天尚未写下盘前计划"
        chat_lines = "\n".join(f"{'【教练】' if m['role'] == 'agent' else '【交易员】'}：{m['content']}" for m in history)

        prompt = (
            "你不是被动的客服，你是交易员专属的 **Proactive Trading Coach（双向波动率与下注推手）**。\n"
            "核心风格：直接、锐利、不说脏话、中英自然混说、站在顶级期权/宏观交易员视角，绝不说“投资有风险请谨慎”之类的废话。\n\n"
            f"### 统一市场体温计（状态：{summary['regimeLabel']}）\n{thermo_block}\n\n"
            f"### VIX 波动率中枢\n{vix_block}\n\n"
            f"### 交易员今日计划\n{plan_block}\n\n"
            f"### 交易员自己写下的交易铁律\n{rules_block}\n\n"
            f"### 交易员近期最痛的 Should Have Done 教训\n{regret_block}\n\n"
            "### 你的核心工作原则（严格遵守）\n"
            "1. **波动率极端放大（regime=extreme，或交易员提到超级波动/暴跌超跌/VIX飙升）**：\n"
            "   - 你必须 **强烈 Push 交易员出手（We MUST find something to do / 一定要 make a bet）**，引用他自己的铁律或踏空教训。\n"
            "   - 给出 2 个 **Bounded Risk, Asymmetric Payoff（下限锁死、上限极高）** 的具体方案（例如固定最大亏损 $500–$1000 或 0.5R 去买 3–5 DTE OTM Call/Put 或现货分批）。\n"
            "2. **波动率极致收窄 / 变盘前兆（regime=squeeze）**：\n"
            "   - 提醒现在处于 **低 IV 甜点期（期权便宜）**，给出 2 个提前埋伏方案（单边突破 Call/Put 或小额 Strangle 双买），严格锁死最大亏损。\n"
            "3. **常规平静日（regime=calm）**：\n"
            "   - 鼓励他果断签字 **Do Nothing**，守住子弹不在垃圾波动里手痒。\n"
            "4. 处于大放大或大收窄窗口时，结尾必须给出 **拍板 3 问：① 押哪个标的 ② 买什么工具/方向 ③ 最大愿意亏多少钱**。\n\n"
            f"### 今日对话记录\n{chat_lines}\n\n"
            "请以【教练】身份直接回复交易员最后一条消息（简洁有力，分点清晰，300字以内，不要套话）："
        )
        try:
            reply_text, used_model = ai.generate_content(prompt, tier="pro")
        except ai.GeminiError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        if not reply_text:
            raise HTTPException(502, "波动教练返回为空")

        conn.execute(
            "INSERT INTO coach_messages(session_day, role, content, regime, created_at) VALUES (?, 'agent', ?, ?, ?)",
            (day, reply_text, summary["regime"], db.now_iso()),
        )
        conn.commit()
        return {
            "ok": True,
            "reply": reply_text,
            "model": used_model,
            "summary": summary,
            "regime": summary["regime"],
            "regimeLabel": summary["regimeLabel"],
            "freshness": summary["freshness"],
            "messages": _coach_messages(day),
        }

    @app.post("/api/coach/commit")
    def coach_commit() -> dict[str, Any]:
        """把今天与主动波动教练的对话直接沉淀为一条可追踪的复盘日记/下注计划。"""
        day = db.today_str()
        msgs = _coach_messages(day)
        if not msgs:
            raise HTTPException(400, "今天还没有和波动教练的对话")
        transcript = "\n\n".join(
            f"{'【波动教练】' if m['role'] == 'agent' else '【我】'}\n{m['content']}" for m in msgs
        )
        summary = morning.build_morning_summary(conn, force=False, allow_network=False)
        movers = [x["symbol"] for x in ((summary.get("thermometer") or {}).get("topLosers") or [])][:4]
        top_syms = movers or ["NVDA", "NDX", "SPX", "SMH"]
        ts = db.now_iso()
        cur = conn.execute(
            """
            INSERT INTO entries(
                trade_date, title, symbol, interval, outcome, pnl,
                bias_tags_json, raw_note, context_note, symbols_json,
                execution_note, should_have_note, takeaway_note,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                day,
                f"{day} 波动教练下注推演（{top_syms[0]}）",
                top_syms[0],
                "15m",
                "review",
                "",
                json.dumps([], ensure_ascii=False),
                transcript,
                "",
                json.dumps(top_syms, ensure_ascii=False),
                "",
                "",
                "",
                ts,
                ts,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM entries WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"ok": True, "entry": db.entry_to_dict(conn, row), "stats": _build_stats(conn)}

    # ---------- Morning Desk (Top-Down K-Line Snapshot) & Daily Plan Sign-Off ----------
    @app.get("/api/morning/summary")
    def api_morning_summary(force: bool = False) -> dict[str, Any]:
        return {"ok": True, **morning.build_morning_summary(conn, force=force)}

    @app.get("/api/morning/charts")
    def api_morning_charts(interval: str = "1wk", section: str = "", force: bool = False) -> dict[str, Any]:
        return {"ok": True, **morning.build_morning_charts(conn, interval=interval, section=section, force=force)}

    @app.get("/api/plans/today")
    def api_plan_today(plan_date: str = "") -> dict[str, Any]:
        d = plan_date.strip() or db.today_str()
        row = conn.execute("SELECT * FROM daily_plans WHERE plan_date=?", (d,)).fetchone()
        hist_rows = conn.execute("SELECT * FROM daily_plans ORDER BY plan_date DESC LIMIT 14").fetchall()
        return {
            "ok": True,
            "plan": db.plan_to_dict(row, d),
            "streak": db.daily_plan_streak(conn),
            "history": [db.plan_to_dict(r) for r in hist_rows],
        }

    @app.post("/api/plans/today")
    def api_plan_upsert(req: DailyPlanUpsertReq) -> dict[str, Any]:
        d = req.plan_date.strip() or db.today_str()
        now = db.now_iso()
        existing = conn.execute("SELECT * FROM daily_plans WHERE plan_date=?", (d,)).fetchone()
        cur_data = db.plan_to_dict(existing, d)

        stance = req.stance if req.stance in ("do_nothing", "watch", "bet") else cur_data["stance"]
        plan_note = req.plan_note if req.plan_note is not None else cur_data["plan_note"]
        step1_note = req.step1_note if req.step1_note is not None else cur_data["step1_note"]
        step2_note = req.step2_note if req.step2_note is not None else cur_data["step2_note"]
        step3_note = req.step3_note if req.step3_note is not None else cur_data["step3_note"]
        market_snapshot = req.market_snapshot if req.market_snapshot is not None else cur_data["market_snapshot"]
        focus_syms = (
            [market.normalize_symbol(s)[0] for s in req.focus_symbols if str(s).strip()][:8]
            if req.focus_symbols is not None
            else cur_data["focus_symbols"]
        )
        plan_followed = (
            req.plan_followed
            if req.plan_followed in ("", "yes", "partial", "no")
            else cur_data["plan_followed"]
        )
        evening_review = req.evening_review if req.evening_review is not None else cur_data["evening_review"]
        has_any_step = bool(step1_note.strip() or step2_note.strip() or step3_note.strip() or plan_note.strip())
        signed_at = cur_data["signed_at"] or (now if (req.sign_morning or has_any_step) else "")
        reviewed_at = cur_data["reviewed_at"]
        if req.commit_evening or (req.evening_review and req.evening_review.strip()):
            reviewed_at = now

        conn.execute(
            """
            INSERT INTO daily_plans(
                plan_date, stance, plan_note, step1_note, step2_note, step3_note, market_snapshot_json,
                focus_symbols_json, plan_followed, evening_review, signed_at, reviewed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_date) DO UPDATE SET
                stance=excluded.stance,
                plan_note=excluded.plan_note,
                step1_note=excluded.step1_note,
                step2_note=excluded.step2_note,
                step3_note=excluded.step3_note,
                market_snapshot_json=excluded.market_snapshot_json,
                focus_symbols_json=excluded.focus_symbols_json,
                plan_followed=excluded.plan_followed,
                evening_review=excluded.evening_review,
                signed_at=excluded.signed_at,
                reviewed_at=excluded.reviewed_at,
                updated_at=excluded.updated_at
            """,
            (
                d,
                stance,
                plan_note.strip(),
                step1_note.strip(),
                step2_note.strip(),
                step3_note.strip(),
                json.dumps(market_snapshot, ensure_ascii=False),
                json.dumps(focus_syms, ensure_ascii=False),
                plan_followed,
                evening_review.strip(),
                signed_at,
                reviewed_at,
                now,
            ),
        )
        conn.commit()

        created_entry = None
        if req.commit_evening and (evening_review.strip() or plan_note.strip()):
            stance_cn = {"do_nothing": "Do Nothing（空仓观望）", "watch": "重点盯盘等 15m", "bet": "准备下非对称 Bet"}[stance]
            follow_cn = {"yes": "完全守住计划", "partial": "部分执行", "no": "破了计划", "": "未标注"}[plan_followed]
            raw_body = (
                f"【{d} 晨间计划】姿态：{stance_cn}\n"
                f"计划备注：{plan_note.strip() or '无'}\n"
                f"【睡前复盘】执行情况：{follow_cn}\n"
                f"{evening_review.strip()}"
            )
            primary_sym = focus_syms[0] if focus_syms else "SPX"
            cur = conn.execute(
                """
                INSERT INTO entries(
                    trade_date, title, symbol, interval, outcome, pnl,
                    bias_tags_json, raw_note, context_note, symbols_json,
                    execution_note, should_have_note, takeaway_note,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    d,
                    f"{d} 每日计划与睡前闭环（{stance_cn.split('（')[0]}）",
                    primary_sym,
                    "15m",
                    "review",
                    "",
                    json.dumps([], ensure_ascii=False),
                    raw_body,
                    "",
                    json.dumps(focus_syms or ["SPX", "NDX", "SMH", "NVDA"], ensure_ascii=False),
                    "",
                    "",
                    "",
                    now,
                    now,
                ),
            )
            conn.commit()
            erow = conn.execute("SELECT * FROM entries WHERE id=?", (cur.lastrowid,)).fetchone()
            created_entry = db.entry_to_dict(conn, erow)

        fresh = conn.execute("SELECT * FROM daily_plans WHERE plan_date=?", (d,)).fetchone()
        return {
            "ok": True,
            "plan": db.plan_to_dict(fresh, d),
            "streak": db.daily_plan_streak(conn),
            "entry": created_entry,
            "stats": _build_stats(conn) if created_entry else None,
        }

    # ---------- Daily Package Calendar (Track Everything) ----------
    @app.get("/api/calendar")
    def api_calendar(month: str = "") -> dict[str, Any]:
        """按月返回每天打包好的 Daily Package（盘前计划 + 市场快照 + 盘后复盘）。"""
        m = month.strip()
        if not re.fullmatch(r"\d{4}-\d{2}", m):
            m = db.today_str()[:7]
        like = f"{m}-%"
        plan_rows = conn.execute("SELECT * FROM daily_plans WHERE plan_date LIKE ? ORDER BY plan_date ASC", (like,)).fetchall()
        entry_rows = conn.execute("SELECT * FROM entries WHERE trade_date LIKE ? ORDER BY trade_date ASC, id DESC", (like,)).fetchall()

        days: dict[str, dict[str, Any]] = {}
        for pr in plan_rows:
            pd = db.plan_to_dict(pr)
            dstr = pd["plan_date"]
            days[dstr] = {
                "date": dstr,
                "plan": pd,
                "entries": [],
            }
        for er in entry_rows:
            ed = db.entry_to_dict(conn, er)
            dstr = ed["trade_date"]
            if dstr not in days:
                days[dstr] = {
                    "date": dstr,
                    "plan": db.plan_to_dict(None, dstr),
                    "entries": [],
                }
            days[dstr]["entries"].append(ed)

        # 为每一天汇总简明 Package 元信息（用于日历格子渲染）
        out_days: dict[str, dict[str, Any]] = {}
        for dstr, pkg in days.items():
            p = pkg["plan"]
            ents = pkg["entries"]
            outcomes = [e["outcome"] for e in ents]
            primary_outcome = (
                "win" if "win" in outcomes else
                ("loss" if "loss" in outcomes else
                ("missed" if "missed" in outcomes else
                ("review" if outcomes else "")))
            )
            tags = []
            for e in ents:
                for t in (e.get("bias_tags") or []):
                    if t not in tags:
                        tags.append(t)
            decision_preview = (p.get("step3_note") or p.get("plan_note") or p.get("step1_note") or "").strip()
            out_days[dstr] = {
                "date": dstr,
                "hasPlan": bool(p.get("has_plan")),
                "hasReview": bool(ents),
                "entryCount": len(ents),
                "primaryOutcome": primary_outcome,
                "pnl": next((e["pnl"] for e in ents if e.get("pnl")), ""),
                "decisionPreview": decision_preview[:40],
                "titlePreview": ents[0]["title"][:36] if ents else "",
                "tags": tags[:4],
                "marketSnapshot": p.get("market_snapshot") or {},
                "plan": p,
                "entries": ents,
            }

        return {
            "ok": True,
            "month": m,
            "today": db.today_str(),
            "days": out_days,
        }

    @app.get("/api/packages/{plan_date}")
    def api_daily_package(plan_date: str) -> dict[str, Any]:
        """获取某一天的完整 Daily Package（盘前计划 + 当日所有复盘文章）。"""
        d = plan_date.strip()
        pr = conn.execute("SELECT * FROM daily_plans WHERE plan_date=?", (d,)).fetchone()
        erows = conn.execute("SELECT * FROM entries WHERE trade_date=? ORDER BY id DESC", (d,)).fetchall()
        return {
            "ok": True,
            "date": d,
            "plan": db.plan_to_dict(pr, d),
            "entries": [db.entry_to_dict(conn, r) for r in erows],
        }

    if STATIC_DIR.is_dir():
        @app.get("/", response_class=HTMLResponse)
        def index() -> HTMLResponse:
            return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

        app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
