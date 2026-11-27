"""Gemini AI 适配层：支持 Gemini REST API、Antigravity CLI (agy) 与 Gemini CLI 三种后端，按任务快慢分层路由。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

import httpx

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# 2026-11 默认分层模型（均可通过环境变量覆盖）：
# - Fast：盘后口述一键归类（低延迟、严格 JSON）
# - Pro：盘前波动教练 & 盘后行为模式审计（高推理、开启 thinking 思考链）
DEFAULT_FAST_MODEL = "gemini-3.8-flash"
DEFAULT_PRO_MODEL = "gemini-4-neon"
DEFAULT_PRO_THINKING = "high"

# Antigravity CLI (`agy`) 默认分层模型（对应 `agy models` 列表，免 API Key）
DEFAULT_AGY_FAST_MODEL = "gemini-3.8-flash-low"
DEFAULT_AGY_PRO_MODEL = "gemini-4-neon-high"

# Gemini CLI 默认分层模型（对应 gemini --list-models 列表）
DEFAULT_GEMINI_CLI_FAST_MODEL = "gemini-3.5-flash"
DEFAULT_GEMINI_CLI_PRO_MODEL = "gemini-2.5-pro"

FAST_TIMEOUT_SEC = 60.0
PRO_TIMEOUT_SEC = 120.0

_VALID_THINKING_LEVELS = {"low", "medium", "high", "xhigh"}
_LEGACY_25_LEVEL_BUDGETS = {
    "low": 2048,
    "medium": 8192,
    "high": -1,
    "xhigh": -1,
}
_AGY_EFFORT_MAP = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}
_CLI_NOISE_PREFIXES = (
    "Data collection is disabled.",
    "Loaded cached credentials.",
    "Warning:",
    "Ripgrep is not available.",
)


class GeminiError(RuntimeError):
    """封装 AI 调用错误与建议的 HTTP 状态码。"""

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


def _version_sort_key(path: Path) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(path))
    return tuple(int(n) for n in nums) if nums else (0,)


def find_agy_bin() -> str | None:
    """查找本机 Antigravity CLI (`agy`) 可执行文件路径。"""
    override = (os.environ.get("AGY_CLI_BIN") or "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        found = shutil.which(override)
        if found:
            return found

    found = shutil.which("agy")
    if found:
        return found

    for cand in (
        Path.home() / ".local" / "bin" / "agy",
        Path("/opt/homebrew/bin/agy"),
        Path("/usr/local/bin/agy"),
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def find_gemini_cli_bin() -> str | None:
    """查找本机 Gemini CLI (`gemini`) 可执行文件路径（兼容 nvm / Homebrew / launchd 环境）。"""
    override = (os.environ.get("GEMINI_CLI_BIN") or "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        found = shutil.which(override)
        if found:
            return found

    found = shutil.which("gemini")
    if found:
        return found

    nvm_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        nvm_cands = sorted(nvm_dir.glob("*/bin/gemini"), key=_version_sort_key, reverse=True)
        for cand in nvm_cands:
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)

    for cand in (
        Path("/opt/homebrew/bin/gemini"),
        Path("/usr/local/bin/gemini"),
        Path.home() / ".local" / "bin" / "gemini",
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def resolve_provider() -> Literal["api", "agy", "gemini-cli", "none"]:
    """解析当前激活的 AI 后端：支持 auto / api / agy / gemini-cli。"""
    raw = (os.environ.get("GEMINI_PROVIDER") or "auto").strip().lower()
    if raw == "api":
        return "api" if get_api_key() else "none"
    if raw in ("agy", "antigravity"):
        return "agy" if find_agy_bin() else "none"
    if raw in ("gemini-cli", "cli", "gemini"):
        return "gemini-cli" if find_gemini_cli_bin() else "none"

    # auto：优先 API Key（延迟最低），其次 Antigravity CLI (agy)，再次 Gemini CLI
    if get_api_key():
        return "api"
    if find_agy_bin():
        return "agy"
    if find_gemini_cli_bin():
        return "gemini-cli"
    return "none"


def is_configured() -> bool:
    return resolve_provider() != "none"


def get_fast_model(provider: str | None = None) -> str:
    prov = provider or resolve_provider()
    if prov == "agy":
        return _clean_model_name(os.environ.get("AGY_FAST_MODEL", ""), DEFAULT_AGY_FAST_MODEL)
    if prov == "gemini-cli":
        raw = os.environ.get("GEMINI_CLI_FAST_MODEL") or os.environ.get("GEMINI_FAST_MODEL") or ""
        return _clean_model_name(raw, DEFAULT_GEMINI_CLI_FAST_MODEL)
    return _clean_model_name(os.environ.get("GEMINI_FAST_MODEL", ""), DEFAULT_FAST_MODEL)


def get_pro_model(provider: str | None = None) -> str:
    prov = provider or resolve_provider()
    if prov == "agy":
        return _clean_model_name(os.environ.get("AGY_PRO_MODEL", ""), DEFAULT_AGY_PRO_MODEL)
    if prov == "gemini-cli":
        raw = os.environ.get("GEMINI_CLI_PRO_MODEL") or os.environ.get("GEMINI_PRO_MODEL") or ""
        return _clean_model_name(raw, DEFAULT_GEMINI_CLI_PRO_MODEL)
    return _clean_model_name(os.environ.get("GEMINI_PRO_MODEL", ""), DEFAULT_PRO_MODEL)


def get_pro_thinking() -> str:
    val = (os.environ.get("GEMINI_PRO_THINKING") or "").strip().lower()
    return val or DEFAULT_PRO_THINKING


def get_model_config() -> dict[str, str]:
    prov = resolve_provider()
    return {
        "provider": prov,
        "fast": get_fast_model(prov),
        "pro": get_pro_model(prov),
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


def _cli_env(bin_path: str) -> dict[str, str]:
    env = os.environ.copy()
    bin_dir = str(Path(bin_path).parent)
    cur_path = env.get("PATH") or ""
    if bin_dir not in cur_path.split(os.pathsep):
        env["PATH"] = f"{bin_dir}{os.pathsep}{cur_path}" if cur_path else bin_dir
    return env


def _extract_json_envelope(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        try:
            obj = json.loads(text[i : j + 1])
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    return None


def _strip_cli_noise(text: str) -> str:
    lines = [
        line
        for line in (text or "").splitlines()
        if not any(line.strip().startswith(prefix) for prefix in _CLI_NOISE_PREFIXES)
    ]
    return "\n".join(lines).strip()


def _extract_cli_error(stdout: str, stderr: str) -> str:
    for blob in (stderr, stdout):
        env = _extract_json_envelope(blob)
        if env:
            err = env.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"]).strip()
            if isinstance(err, str) and err.strip():
                return err.strip()
    cleaned = _strip_cli_noise(stderr) or _strip_cli_noise(stdout)
    return cleaned[:260] or "CLI 返回非零退出码"


def _generate_via_api(
    prompt: str,
    *,
    tier: Literal["fast", "pro"],
    response_mime_type: str | None,
    req_timeout: float,
) -> tuple[str, str]:
    key = get_api_key()
    if not key:
        raise GeminiError(400, "未检测到 GOOGLE_API_KEY 或 GEMINI_API_KEY 环境变量")

    model = get_fast_model("api") if tier == "fast" else get_pro_model("api")
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


def _generate_via_agy_cli(
    prompt: str,
    *,
    tier: Literal["fast", "pro"],
    req_timeout: float,
) -> tuple[str, str]:
    agy_bin = find_agy_bin()
    if not agy_bin:
        raise GeminiError(400, "未检测到 Antigravity CLI (`agy`)，请先安装并登录 `agy`，或配置 GOOGLE_API_KEY")

    model = get_fast_model("agy") if tier == "fast" else get_pro_model("agy")
    effort = "low" if tier == "fast" else _AGY_EFFORT_MAP.get(get_pro_thinking(), "high")
    model_label = f"agy:{model}"

    cmd = [
        agy_bin,
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--disable-slash-commands",
    ]
    if not re.search(r"-(?:low|medium|high|xhigh|max)$", model.lower()):
        cmd.extend(["--effort", effort])

    try:
        proc = subprocess.run(
            cmd,
            cwd=tempfile.gettempdir(),
            env=_cli_env(agy_bin),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=req_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GeminiError(502, f"Antigravity CLI（{model_label}）执行超时（>{int(req_timeout)}s）") from exc
    except OSError as exc:
        raise GeminiError(502, f"启动 Antigravity CLI 失败：{exc}") from exc

    envelope = _extract_json_envelope(proc.stdout)
    if proc.returncode != 0:
        detail = _extract_cli_error(proc.stdout, proc.stderr)
        raise GeminiError(502, f"Antigravity CLI（{model_label}）调用失败：{detail}")

    if envelope:
        status = str(envelope.get("status") or "").upper()
        if status and status != "SUCCESS":
            err = str(envelope.get("error") or status)
            raise GeminiError(502, f"Antigravity CLI（{model_label}）返回状态 {status}：{err}")
        text = str(envelope.get("response") or "").strip()
        return text, model_label

    return _strip_cli_noise(proc.stdout), model_label


def _generate_via_gemini_cli(
    prompt: str,
    *,
    tier: Literal["fast", "pro"],
    req_timeout: float,
) -> tuple[str, str]:
    cli_bin = find_gemini_cli_bin()
    if not cli_bin:
        raise GeminiError(400, "未检测到 Gemini CLI (`gemini`)，请先安装或配置 GOOGLE_API_KEY / Antigravity CLI (`agy`)")

    model = get_fast_model("gemini-cli") if tier == "fast" else get_pro_model("gemini-cli")
    cmd = [cli_bin, "--skip-trust", "-m", model, "-o", "json", "-p", prompt]

    try:
        proc = subprocess.run(
            cmd,
            cwd=tempfile.gettempdir(),
            env=_cli_env(cli_bin),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=req_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GeminiError(502, f"Gemini CLI（{model}）执行超时（>{int(req_timeout)}s）") from exc
    except OSError as exc:
        raise GeminiError(502, f"启动 Gemini CLI 失败：{exc}") from exc

    if proc.returncode != 0:
        detail = _extract_cli_error(proc.stdout, proc.stderr)
        hint = ""
        if "no longer supported for Gemini Code Assist for individuals" in detail:
            hint = "（Google 已停止 Gemini CLI 个人 OAuth 登录，请安装并登录 Antigravity CLI `agy`，或配置 GOOGLE_API_KEY）"
        elif "cloudaicompanion" in detail or "GOOGLE_CLOUD_PROJECT" in detail:
            hint = "（请检查 GCP 项目权限，或改用 Antigravity CLI `agy` / GOOGLE_API_KEY）"
        raise GeminiError(502, f"Gemini CLI（{model}）调用失败：{detail}{hint}")

    envelope = _extract_json_envelope(proc.stdout)
    if envelope and "response" in envelope:
        return str(envelope.get("response") or "").strip(), f"gemini-cli:{model}"
    return _strip_cli_noise(proc.stdout), f"gemini-cli:{model}"


def generate_content(
    prompt: str,
    *,
    tier: Literal["fast", "pro"],
    response_mime_type: str | None = None,
    timeout: float | None = None,
) -> tuple[str, str]:
    """按层级与当前可用后端调用 AI，返回 (text, used_model)。"""
    provider = resolve_provider()
    req_timeout = timeout if timeout is not None else (FAST_TIMEOUT_SEC if tier == "fast" else PRO_TIMEOUT_SEC)

    if provider == "api":
        return _generate_via_api(prompt, tier=tier, response_mime_type=response_mime_type, req_timeout=req_timeout)
    if provider == "agy":
        return _generate_via_agy_cli(prompt, tier=tier, req_timeout=req_timeout)
    if provider == "gemini-cli":
        return _generate_via_gemini_cli(prompt, tier=tier, req_timeout=req_timeout)

    raise GeminiError(
        400,
        "未检测到可用的 AI 后端：请配置 GOOGLE_API_KEY / GEMINI_API_KEY，或安装并登录 Antigravity CLI (`agy`) / Gemini CLI (`gemini`)",
    )
