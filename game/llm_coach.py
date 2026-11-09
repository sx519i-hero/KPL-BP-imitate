"""v4 · 外部大模型 AI 教练（DeepSeek / 任何 OpenAI 兼容 chat/completions 接口）。

流程：把当前 BP 局面打包成 JSON → POST 到用户配置的接口 → 解析
``{"action": "ban"|"pick", "hero_name": "英雄名"}`` → 交给调用方执行。

稳定性要求（按需求 3.3）：
* 解析失败 **重试 1 次**，仍失败则回退内置 AI，并给出「大模型返回异常，使用内置 AI 兜底」；
* 超时 / token 错误 / 网络不通都要有明确提示，不能让游戏卡死转圈；
* 只用标准库 urllib，不引入新依赖。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from . import ai_config

SYSTEM_PROMPT = (
    "你是一名 KPL 战队教练。当前是 KPL BP 环节。\n"
    "以下是当前局面 JSON：{game_state}\n"
    '请你决定：{{"action": "ban" 或 "pick", "hero_name": "英雄名"}}\n'
    "只返回 JSON，不要解释。"
)

_JSON_RE = re.compile(r"\{[^{}]*\}")


class LLMError(Exception):
    """大模型调用失败。``kind`` 用于给前端明确提示。"""

    def __init__(self, message: str, kind: str = "error", status: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _http_post(endpoint: str, payload: Dict[str, Any], api_key: str,
               timeout: float) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": "Bearer %s" % api_key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:                                   # noqa: BLE001
            detail = ""
        if exc.code in (401, 403):
            raise LLMError("API 密钥无效或无权限（HTTP %d）%s" % (exc.code, detail),
                           "auth", exc.code) from exc
        if exc.code == 429:
            raise LLMError("请求太频繁或额度不足（HTTP 429）%s" % detail, "quota", 429) from exc
        raise LLMError("接口返回错误（HTTP %d）%s" % (exc.code, detail), "http", exc.code) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise LLMError("请求大模型超时（%.0f 秒）" % timeout, "timeout") from exc
        raise LLMError("连不上大模型接口：%s" % reason, "network") from exc
    except TimeoutError as exc:
        raise LLMError("请求大模型超时（%.0f 秒）" % timeout, "timeout") from exc
    except OSError as exc:
        raise LLMError("网络异常：%s" % exc, "network") from exc

    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LLMError("接口返回的不是 JSON", "bad_json") from exc


def _extract_content(resp: Dict[str, Any]) -> str:
    choices = resp.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        msg = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
        text = first.get("text")
        if isinstance(text, str) and text.strip():
            return text
    for key in ("output_text", "content", "result"):
        value = resp.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise LLMError("接口响应里找不到模型输出（缺少 choices[0].message.content）", "bad_json")


# 「服务繁忙」类文案：HTTP 可能仍是 200，但 content 里是一句人话，
# 需求要求这类情况必须识别出来并回退内置 AI，而不是当成正常决策。
_BUSY_HINTS = (
    "too busy",
    "service is busy",
    "busy",
    "overloaded",
    "rate limit",
    "try again",
    "temporarily unavailable",
    "服务繁忙",
    "系统繁忙",
    "请稍后",
    "稍后再试",
    "请求过于频繁",
)


def _looks_busy(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False
    return any(hint in low for hint in _BUSY_HINTS)


def _parse_decision(text: str) -> Dict[str, str]:
    candidates: List[str] = []
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    candidates.append(stripped)
    candidates.extend(_JSON_RE.findall(stripped))
    # 只有「完全解析不出 JSON」且文本像服务繁忙时，才报 busy，避免误伤
    for chunk in candidates:
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        action = str(data.get("action") or "").strip().lower()
        hero = data.get("hero_name") or data.get("hero") or data.get("name")
        hero = str(hero or "").strip()
        if action in ("ban", "pick") and hero:
            return {"action": action, "hero_name": hero}
    if _looks_busy(text):
        raise LLMError("大模型服务繁忙：%s" % (text or "").strip()[:80], "busy")
    raise LLMError("模型没有返回可解析的 {\"action\",\"hero_name\"} JSON", "bad_json")


def _available_names(available: List[Dict[str, Any]]) -> List[str]:
    names = []
    for hero in available or []:
        name = hero.get("cname")
        if name:
            names.append(str(name))
    return names


def build_game_state(match: Dict[str, Any], side: str, league_ctx=None,
                     action: Optional[str] = None) -> Dict[str, Any]:
    """打包给大模型的局面 JSON（只含真实数据，不臆造）。"""
    picks = match.get("picks") or {}
    bans = match.get("bans") or {}
    available = match.get("available") or []

    def names(items: List[Any]) -> List[str]:
        out = []
        for ename in items or []:
            name = None
            if league_ctx is not None:
                try:
                    name = league_ctx.cname(ename)
                except Exception:                           # noqa: BLE001
                    name = None
            out.append(name or str(ename))
        return out

    pool = []
    for hero in available[:80]:
        item = {"name": hero.get("cname")}
        if hero.get("lane_list"):
            item["lanes"] = list(hero["lane_list"][:3])
        if isinstance(hero.get("win_rate"), (int, float)):
            item["win_rate"] = round(float(hero["win_rate"]), 2)
        if isinstance(hero.get("ban_rate"), (int, float)):
            item["ban_rate"] = round(float(hero["ban_rate"]), 2)
        pool.append(item)

    return {
        "league": match.get("league_name") or match.get("league_id"),
        "stage": "BP 第 %s 步 / 共 %s 步" % (match.get("step"), match.get("total_steps")),
        "current_side": side,
        "current_side_cn": "蓝方" if side == "blue" else "红方",
        "current_action": action or match.get("action"),
        "is_tiebreak": bool(match.get("is_tiebreak")),
        "blue_team": (match.get("blue_team") or {}).get("name")
                     if isinstance(match.get("blue_team"), dict) else match.get("blue_team"),
        "red_team": (match.get("red_team") or {}).get("name")
                    if isinstance(match.get("red_team"), dict) else match.get("red_team"),
        "blue_bans": names(bans.get("blue")),
        "red_bans": names(bans.get("red")),
        "blue_picks": names(picks.get("blue")),
        "red_picks": names(picks.get("red")),
        "available_heroes": _available_names(available),
        "available_pool_sample": pool,
        "available_count": len(available),
    }


def _resolve_hero(name: str, available: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    target = name.strip()
    if not target:
        return None
    for hero in available or []:
        if hero.get("cname") == target:
            return hero
    lowered = target.lower()
    for hero in available or []:
        if str(hero.get("cname", "")).lower() == lowered:
            return hero
    for hero in available or []:
        cname = str(hero.get("cname", ""))
        if cname and (cname in target or target in cname):
            return hero
    return None


def decide(match: Dict[str, Any], side: str, league_ctx=None, *,
           action: Optional[str] = None, logger=None) -> Dict[str, Any]:
    """让大模型决定一手。

    返回 ``{ename, action, source, note, attempts, latency_ms}``；
    ``source == "fallback"`` 表示已回退到内置 AI（附 ``error`` 说明原因）。
    """
    log = logger
    cfg = ai_config.load_config()["llm"]
    state = ai_config.llm_state()
    if not state["ready"]:
        raise LLMError("请先在「设置」里配置大模型 API（%s）" % "、".join(state["missing"]), "no_config")

    desired_action = action or match.get("action") or "pick"
    game_state = build_game_state(match, side, league_ctx, desired_action)
    prompt = SYSTEM_PROMPT.format(game_state=json.dumps(game_state, ensure_ascii=False))
    payload = {
        "model": cfg["model"],
        "temperature": cfg["temperature"],
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请给出这一手决定，只返回 JSON。"},
        ],
    }
    timeout = float(cfg.get("timeout") or 20)
    started = time.time()
    last_error: Optional[LLMError] = None
    for attempt in range(2):                                # 失败重试 1 次
        try:
            resp = _http_post(cfg["endpoint"], payload, cfg["api_key"], timeout)
            content = _extract_content(resp)
            decision = _parse_decision(content)
            hero = _resolve_hero(decision["hero_name"], match.get("available") or [])
            if hero is None:
                raise LLMError("模型选了不在可选池里的英雄：%s" % decision["hero_name"], "bad_pick")
            return {
                "ename": int(hero["ename"]),
                "cname": hero.get("cname"),
                "action": decision["action"] if decision["action"] == desired_action else desired_action,
                "source": "llm",
                "note": "大模型决策（%s，第 %d 次请求）" % (cfg["model"], attempt + 1),
                "attempts": attempt + 1,
                "latency_ms": int((time.time() - started) * 1000),
            }
        except LLMError as exc:
            last_error = exc
            if exc.kind in ("no_config",):
                raise
            if attempt == 0 and exc.kind in ("bad_json", "bad_pick", "http", "busy"):
                if log:
                    log.info("[llm] 第 1 次解析失败（%s），重试一次", exc)
                continue
            break

    # 兜底：内置 AI
    try:
        from . import ai_coach
        fallback = ai_coach.choose_action(match, side, league_ctx, action=desired_action)
    except Exception as exc:                                # noqa: BLE001
        raise LLMError("大模型异常（%s），且内置 AI 兜底也失败：%s"
                       % (last_error, exc), "fallback_failed") from exc
    ename = fallback.get("ename") if isinstance(fallback, dict) else fallback
    note = "大模型返回异常，使用内置 AI 兜底（%s）" % (last_error or "未知错误")
    return {
        "ename": int(ename),
        "cname": None,
        "action": desired_action,
        "source": "fallback",
        "note": note,
        "error": str(last_error) if last_error else "",
        "error_kind": getattr(last_error, "kind", "error"),
        "attempts": 2,
        "latency_ms": int((time.time() - started) * 1000),
    }


def probe() -> Dict[str, Any]:
    """设置页「测试连接」：真实发一次极短请求，返回结果而不抛异常给前端 500。"""
    state = ai_config.llm_state()
    if not state["ready"]:
        return {"ok": False, "kind": "no_config",
                "error": "请先在「设置」里配置（%s）" % "、".join(state["missing"])}
    cfg = ai_config.load_config()["llm"]
    started = time.time()
    try:
        resp = _http_post(cfg["endpoint"], {
            "model": cfg["model"],
            "temperature": 0,
            # v31：以前这里让模型回 {"ok":true}，却又按「决策 JSON」去解析，
            # 模型答对了也会被判失败；改成直接要一个决策样例，才是真校验。
            "messages": [{"role": "user",
                          "content": '只回复 {"action":"pick","hero_name":"孙尚香"} 这段 JSON'}],
        }, cfg["api_key"], min(15, float(cfg.get("timeout") or 20)))
        content = _extract_content(resp)
        latency = int((time.time() - started) * 1000)
        # 光「HTTP 200」不算通过：模型必须真的能返回可解析的决策 JSON，
        # 否则对局里一定回退内置 AI。这里把真实解析结果也报给设置页。
        try:
            decision = _parse_decision(content)
        except LLMError as exc:
            return {"ok": False, "kind": exc.kind, "model": cfg["model"],
                    "latency_ms": latency, "reply": content[:200],
                    "error": "%s（模型确实连上了，但对局时会回退内置 AI）" % exc}
        return {"ok": True, "model": cfg["model"], "latency_ms": latency,
                "reply": content[:200], "decision": decision}
    except LLMError as exc:
        return {"ok": False, "kind": exc.kind, "error": str(exc),
                "latency_ms": int((time.time() - started) * 1000)}
