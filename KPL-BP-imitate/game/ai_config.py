"""v4 · 本地配置读写（外部大模型 AI）。

配置文件：``data/ai_config.json``。**只存本机，不上传任何服务器。**
API 密钥做 base64 简单混淆（不是为了安全，只是别明文写在 JSON 里太显眼）。

本模块只用标准库，任何异常都向上抛 ``ConfigError`` 或返回默认值，不阻塞游戏。
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import os
import threading
from datetime import datetime
from typing import Any, Dict

from . import paths  # v10：打包后数据目录在 exe 同级

BASE_DIR = paths.resource_dir()
CONFIG_PATH = paths.data_path("ai_config.json")

DEFAULTS: Dict[str, Any] = {
    "llm": {
        "enabled": False,
        "endpoint": "https://api.deepseek.com/v1/chat/completions",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.7,
        "timeout": 20,
        # v7 需求 3：外部大模型固定只执教 1 支战队（single_team）；
        # 旧值 all/final 仅作历史存档兼容读取，运行时一律按 single_team 解释。
        "league_scope": "single_team",
    },
}

_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {"mtime": None, "data": None}


class ConfigError(Exception):
    """配置读写失败。"""


def _mask(key: str) -> str:
    return "b64:" + base64.b64encode((key or "").encode("utf-8")).decode("ascii")


def _unmask(raw: str) -> str:
    raw = str(raw or "")
    if raw.startswith("b64:"):
        try:
            return base64.b64decode(raw[4:].encode("ascii")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return ""
    return raw


def _normalize(raw: Any) -> Dict[str, Any]:
    data = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        return data

    llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}
    out_llm = data["llm"]
    out_llm["enabled"] = bool(llm.get("enabled", out_llm["enabled"]))
    endpoint = str(llm.get("endpoint") or out_llm["endpoint"]).strip()
    out_llm["endpoint"] = endpoint or out_llm["endpoint"]
    out_llm["api_key"] = _unmask(llm.get("api_key"))
    out_llm["model"] = str(llm.get("model") or out_llm["model"]).strip() or out_llm["model"]
    try:
        temp = float(llm.get("temperature", out_llm["temperature"]))
    except (TypeError, ValueError):
        temp = out_llm["temperature"]
    out_llm["temperature"] = max(0.0, min(2.0, temp))
    try:
        timeout = int(float(llm.get("timeout", out_llm["timeout"])))
    except (TypeError, ValueError):
        timeout = out_llm["timeout"]
    out_llm["timeout"] = max(3, min(120, timeout))
    # v7 需求 3：外部大模型固定只执教 1 支战队。旧存档里的 all/final 一律迁移为 single_team，
    # 避免「外部 LLM 控制全部队伍」的旧逻辑继续生效（读档即迁移，不改动旧存档其它字段）。
    scope = str(llm.get("league_scope") or out_llm["league_scope"])
    if scope in ("all", "final"):
        scope = "single_team"
    out_llm["league_scope"] = scope if scope in ("single_team",) else "single_team"

    return data


def load_config() -> Dict[str, Any]:
    """读配置（带 mtime 缓存）。文件不存在或损坏都返回默认值，不抛异常。"""
    with _LOCK:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            return copy.deepcopy(DEFAULTS)
        if _CACHE["mtime"] == mtime and _CACHE["data"] is not None:
            return copy.deepcopy(_CACHE["data"])
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = None
        data = _normalize(raw)
        _CACHE["mtime"] = mtime
        _CACHE["data"] = data
        return copy.deepcopy(data)


def public_config() -> Dict[str, Any]:
    """给前端的配置：密钥打码，绝不下发明文。"""
    data = load_config()
    key = data["llm"]["api_key"]
    data["llm"]["api_key"] = ""
    data["llm"]["api_key_set"] = bool(key)
    data["llm"]["api_key_hint"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else "")
    data["updated_at"] = _updated_at()
    data["path"] = os.path.relpath(CONFIG_PATH, BASE_DIR).replace("\\", "/")
    return data


def _updated_at() -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(CONFIG_PATH)).isoformat(timespec="seconds")
    except OSError:
        return ""


def save_config(patch: Any) -> Dict[str, Any]:
    """合并保存。``patch.llm.api_key`` 为空字符串表示「保持原密钥不变」。"""
    if not isinstance(patch, dict):
        raise ConfigError("配置格式不正确")
    with _LOCK:
        current = load_config()
        if isinstance(patch.get("llm"), dict):
            src = patch["llm"]
            for key in ("enabled", "endpoint", "model", "temperature", "timeout", "league_scope"):
                if key in src:
                    current["llm"][key] = src[key]
            if "api_key" in src:
                raw_key = str(src.get("api_key") or "").strip()
                if raw_key and raw_key != "******":
                    current["llm"]["api_key"] = raw_key
                elif src.get("clear_api_key"):
                    current["llm"]["api_key"] = ""

        data = _normalize(current)
        payload = {
            "llm": {k: v for k, v in data["llm"].items() if k != "api_key"},
        }
        payload["llm"]["api_key"] = _mask(data["llm"]["api_key"])
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except OSError as exc:
            raise ConfigError("配置写入失败：%s" % exc) from exc
        _CACHE["mtime"] = None
        _CACHE["data"] = None
    return public_config()


def llm_ready() -> bool:
    """外部大模型是否可用（开关打开 + 密钥 + 地址 + 模型齐全）。"""
    data = load_config()["llm"]
    return bool(data.get("enabled") and data.get("api_key")
                and data.get("endpoint") and data.get("model"))


def llm_state() -> Dict[str, Any]:
    data = load_config()["llm"]
    missing = []
    if not data.get("enabled"):
        missing.append("开关未开启")
    if not data.get("endpoint"):
        missing.append("缺少接口地址")
    if not data.get("api_key"):
        missing.append("缺少 API 密钥")
    if not data.get("model"):
        missing.append("缺少模型名称")
    return {"ready": not missing, "missing": missing,
            "enabled": bool(data.get("enabled")), "model": data.get("model"),
            "endpoint": data.get("endpoint")}


    return {"enabled": bool(data.get("enabled")), "url": data.get("url"),
            "timeout": data.get("timeout")}
