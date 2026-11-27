"""Gemini AI 适配层：快慢双模型分层路由（口述归类用 Fast，波动教练与行为审计用 Pro + 深度思考）。"""
from __future__ import annotations

import os
import re
from typing import Any, Literal

import httpx

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# 2026-11 默认分层模型（均可通过环境变量覆盖）：
# - Fast：盘后口述一键归类（低延迟、严格 JSON）
# - Pro：盘前波动教练 & 盘后行为模式审计（高推理、开启 thinking 思考链）
DEFAULT_FAST_MODEL = "gemini-3.8-flash"
DEFAULT_PRO_MODEL = "gemini-4-neon"
DEFAULT_PRO_THINKING = "high"

FAST_TIMEOUT_SEC = 60.0
PRO_TIMEOUT_SEC = 120.0

_VALID_THINKING_LEVELS = {"low", "medium", "high", "xhigh"}
_LEGACY_25_LEVEL_BUDGETS = {
    "low": 2048,
    "medium": 8192,
    "high": -1,
    "xhigh": -1,
}


class GeminiError(RuntimeError):
    """封装 Gemini API 调用错误与建议的 HTTP 状态码。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _clean_model_name(raw: str, fallback: str) -> str:
    name = (raw or "").strip()
    if name.startswith("models/"):
        name = name[len("models/") :]
    return name or fallback


def get_api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


def get_fast_model() -> str:
    return _clean_model_name(os.environ.get("GEMINI_FAST_MODEL", ""), DEFAULT_FAST_MODEL)


def get_pro_model() -> str:
    return _clean_model_name(os.environ.get("GEMINI_PRO_MODEL", ""), DEFAULT_PRO_MODEL)


def get_pro_thinking() -> str:
    val = (os.environ.get("GEMINI_PRO_THINKING") or "").strip().lower()
    return val or DEFAULT_PRO_THINKING


def get_model_config() -> dict[str, str]:
    return {
        "fast": get_fast_model(),
        "pro": get_pro_model(),
        "proThinking": get_pro_thinking(),
    }


def _is_gemini_25(model: str) -> bool:
    return "gemini-2.5" in model.lower()


def _is_gemini_3_plus(model: str) -> bool:
    return bool(re.search(r"gemini-(?:[3-9]|\d{2,})(?:[.\-]|$)", model.lower()))


def build_generation_config(
    model: str,
    *,
    tier: Literal["fast", "pro"],
    response_mime_type: str | None = None,
    pro_thinking: str | None = None,
) -> dict[str, Any]:
    """根据模型代际与任务层级生成兼容的 generationConfig（自动适配 2.5 thinkingBudget 与 3+/4 thinkingLevel）。"""
    cfg: dict[str, Any] = {}
    if response_mime_type:
        cfg["responseMimeType"] = response_mime_type

    if _is_gemini_25(model):
        if tier == "fast":
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
        else:
            raw_th = (pro_thinking if pro_thinking is not None else get_pro_thinking()).strip().lower()
            try:
                budget = int(raw_th)
            except ValueError:
                budget = _LEGACY_25_LEVEL_BUDGETS.get(raw_th, -1)
            cfg["thinkingConfig"] = {"thinkingBudget": budget}
    elif _is_gemini_3_plus(model):
        if tier == "fast":
            # gemini-3.8-flash 等模型不支持 minimal，使用 LOW 保证最快速度与兼容性
            cfg["thinkingConfig"] = {"thinkingLevel": "LOW"}
        else:
            raw_th = (pro_thinking if pro_thinking is not None else get_pro_thinking()).strip().lower()
            level = raw_th.upper() if raw_th in _VALID_THINKING_LEVELS else "HIGH"
            cfg["thinkingConfig"] = {"thinkingLevel": level}

    return cfg


def extract_text(payload: dict[str, Any]) -> str:
    """提取模型最终回答文本，过滤掉 thought=true 的内部思考链分片。"""
    candidates = payload.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    return "".join(
        str(p.get("text") or "")
        for p in parts
        if isinstance(p, dict) and not p.get("thought")
    ).strip()


def generate_content(
    prompt: str,
    *,
    tier: Literal["fast", "pro"],
    response_mime_type: str | None = None,
    timeout: float | None = None,
) -> tuple[str, str]:
    """按层级调用 Gemini generateContent 接口，返回 (text, used_model)。"""
    key = get_api_key()
    if not key:
        raise GeminiError(400, "未检测到 GOOGLE_API_KEY 或 GEMINI_API_KEY 环境变量")

    model = get_fast_model() if tier == "fast" else get_pro_model()
    req_timeout = timeout if timeout is not None else (FAST_TIMEOUT_SEC if tier == "fast" else PRO_TIMEOUT_SEC)
    gen_cfg = build_generation_config(model, tier=tier, response_mime_type=response_mime_type)

    body: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
    if gen_cfg:
        body["generationConfig"] = gen_cfg

    try:
        resp = httpx.post(
            f"{GEMINI_API_BASE}/{model}:generateContent",
            params={"key": key},
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=req_timeout,
        )
    except httpx.HTTPError as exc:
        raise GeminiError(502, f"连接 Gemini API（{model}）超时或网络异常：{exc}") from exc

    if resp.status_code >= 400:
        hint = ""
        if tier == "pro" and resp.status_code in (403, 404):
            hint = f"（若当前 API Key 为免费层或未开通 {model}，可通过环境变量 GEMINI_PRO_MODEL={DEFAULT_FAST_MODEL} 切换）"
        raise GeminiError(502, f"Gemini API（{model}）返回 {resp.status_code}：{resp.text[:200]}{hint}")

    return extract_text(resp.json()), model
