# -*- coding: utf-8 -*-
"""KPL 模拟 BP 小游戏 —— Flask 后端。

唯一事实来源：`docs/DATA_CONTRACT.md`（v1）。

本文件只负责 HTTP 层：路由、参数校验、内存态 match 仓库、响应装配。
- 数据读取：**只**通过 `game.data_loader.load_*`（`DATA_DIR` 由 data_loader 统一解析），
  本文件不手动拼 data/ 路径、不硬编码任何 KPL 数值。
- BP 规则 / AI 决策 / 阵容评分：`game.bp_engine` / `game.ai_coach` / `game.scorer`。
- `game/` 未交付时降级：`/api/bp/*` 返回 503，列表接口返回空态，静态页仍可用。

依赖：标准库 + flask。
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import math
import os
import random
import re
import secrets
import sys
import threading
import time
import traceback
from datetime import date, datetime

from flask import Flask, Response, render_template, request, send_file
from werkzeug.exceptions import HTTPException

# --------------------------------------------------------------------------- #
# 常量（均为非业务数值：TTL、枚举、文案）
# --------------------------------------------------------------------------- #

APP_VERSION = "8.0.0"

# --------------------------------------------------------------------------- #
# 日志（v4/v5 需求：新增功能全部加调试日志）
#   Flask 非 debug 模式下 app.logger 默认只输出 WARNING，会把这里大量
#   logger.info(...) 的排查信息吞掉。统一开到 INFO，方便用户把控制台输出
#   直接贴给我们定位「联机 / 大模型 / 资源下载 / 评分」问题。
#   （想安静可以把环境变量 KPL_LOG_LEVEL 设成 WARNING）
# --------------------------------------------------------------------------- #
LOG_LEVEL = (os.environ.get("KPL_LOG_LEVEL") or "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

def _resolve_base_dir() -> str:
    """v10：PyInstaller 打包成 exe 后，资源目录是 sys._MEIPASS（onedir 时即 _internal）。"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return meipass
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _resolve_base_dir()
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# v10：可写数据目录（打包成 exe 后是 exe 同级的 data/，存档不会丢）
try:
    from game import paths as _paths_mod

    DATA_DIR = _paths_mod.data_dir()
except Exception:  # noqa: BLE001  打包早期/文件缺失时退回资源目录
    _paths_mod = None
    DATA_DIR = os.path.join(BASE_DIR, "data")

# v33：图片资源也外置（exe 同级 images/，源码运行即 static/images）
try:
    IMAGES_DIR = _paths_mod.images_dir() if _paths_mod is not None else os.path.join(STATIC_DIR, "images")
except Exception:  # noqa: BLE001
    IMAGES_DIR = os.path.join(STATIC_DIR, "images")

MATCH_TTL_SECONDS = 30 * 60          # match 内存态滑动过期（30 分钟）
# v52：小内存服务器上再兜一层硬上限 —— 同时存在的对局最多这么多，超出按最久未操作淘汰。
MATCH_MAX_COUNT = int(os.environ.get("KPL_MATCH_MAX", "40") or 40)
MATCH_SWEEP_MIN_INTERVAL = 30.0      # 清理扫描的最小间隔（秒）

ALLOWED_BO = (1, 3, 5, 7, 9)   # v3：年度总决赛 BO9
ALLOWED_MODES = ("ai", "free")
ALLOWED_SIDES = ("blue", "red")
ALLOWED_DIFFICULTIES = ("rookie", "pro", "boss")
ALLOWED_ACTIONS = ("ban", "pick")

# 契约 v2 §3.2：phase 枚举扩展为 bp -> result -> side_choice -> bp ... -> done
PHASE_BP = "bp"
PHASE_RESULT = "result"
PHASE_SIDE_CHOICE = "side_choice"
PHASE_DONE = "done"
# v20：巅峰对决盲选（双方同时各选 5 人，没有 ban、可撞英雄）
PHASE_TIEBREAK_PICK = "tiebreak_pick"
ALLOWED_PHASES = (PHASE_BP, PHASE_RESULT, PHASE_SIDE_CHOICE, PHASE_DONE, PHASE_TIEBREAK_PICK)

# 只有这些阶段才允许触达 BP（契约 v2 §3.3：side_choice 期间禁止 BP 动作）
BP_PHASES = (PHASE_BP, PHASE_TIEBREAK_PICK)

# 契约 v2 §2.1 的固定值（不是数据，是标注字段）
LANE_SOURCE = "kpl_battle_position"

DEFAULT_PROFILE = {
    "nickname": "教练",
    "total_games": 0,
    "wins": 0,
    "losses": 0,
    "hero_usage": {},
    "last_played": None,
}

# --------------------------------------------------------------------------- #
# Flask 应用
# --------------------------------------------------------------------------- #

# v10：用绝对路径，保证打包成 exe 后也能找到 templates / static
app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)

# 让 app.logger 也跟着输出 INFO（Flask 会给它单独挂 handler）
try:
    app.logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
except Exception:  # pragma: no cover
    pass

# 中文不转义（契约 §2）。Flask >= 2.2 用 app.json；老版本降级到 config。
try:
    app.json.ensure_ascii = False
    app.json.sort_keys = False
except Exception:  # pragma: no cover
    try:
        app.config["JSON_AS_ASCII"] = False
        app.config["JSON_SORT_KEYS"] = False
    except Exception:
        pass


#: v23：静态资源缓存策略
#:  - 图片（英雄头像 / 战队 logo）构建期就不变 → 30 天 immutable，翻页不再重发
#:    100+ 个条件请求，这是「点完要等半天图片才齐」的主因。
#:  - css/js 每次升级都会变，不能给长 max-age（否则用户看到旧脚本），
#:    但 ETag 304 很便宜，所以走「每次校验」。
_STATIC_IMMUTABLE_DIRS = ("/static/images/",)
_STATIC_IMMUTABLE_MAX_AGE = 60 * 60 * 24 * 30   # 图片：30 天
_GZIP_MIN_BYTES = 1024
_GZIP_TYPES = ("text/", "application/json", "application/javascript",
               "text/javascript", "image/svg+xml")


def _maybe_gzip(resp):
    """v23：本地服务也压一下 JSON / HTML / CSS / JS，手机上明显更快。

    只压超过 1KB 的文本响应；已经压过或客户端不支持 gzip 的直接跳过。
    """
    try:
        if resp.headers.get("Content-Encoding"):
            return resp
        # send_file() 出来的静态文件是 direct_passthrough，必须先物化才能压缩
        if getattr(resp, "direct_passthrough", False):
            resp.direct_passthrough = False
        raw = resp.get_data()
        if len(raw) < _GZIP_MIN_BYTES:
            return resp
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not ctype.startswith(_GZIP_TYPES):
            return resp
        if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
            return resp
        packed = gzip.compress(raw, 5)
        if len(packed) >= len(raw):
            return resp
        resp.set_data(packed)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(packed))
        resp.headers.add("Vary", "Accept-Encoding")
    except Exception:      # pragma: no cover —— 压缩失败绝不能影响正常响应
        pass
    return resp


@app.after_request
def _disable_api_cache(resp):
    """API 响应禁止缓存（拿到的永远是当前内存态）；静态资源则尽量长效缓存。"""
    path = request.path or ""
    if path.startswith("/api/") or resp.mimetype == "application/json":
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers.pop("Expires", None)
        return _maybe_gzip(resp)
    if path.startswith("/static/"):
        if path.startswith(_STATIC_IMMUTABLE_DIRS):
            resp.headers["Cache-Control"] = "public, max-age=%d, immutable" % _STATIC_IMMUTABLE_MAX_AGE
        else:
            # css/js：每次都校验（ETag 命中就是 304，代价极小），保证升级后不会拿到旧脚本
            resp.headers["Cache-Control"] = "no-cache"
        resp.headers.pop("Expires", None)
        return _maybe_gzip(resp)
    return _maybe_gzip(resp)


# --------------------------------------------------------------------------- #
# game/ 包（engine-dev 交付）—— 缺失时降级
# --------------------------------------------------------------------------- #

# 逐个导入：game/ 可能只交付了其中一部分（如 data_loader 先到、bp_engine 后到）。
# 一次 `from game import a, b, c` 是原子的——任一缺失会让三者全部为 None，
# 导致列表接口也无谓地退化空态。所以这里必须分开导入、分开记录错误。
_IMPORT_ERRORS = {}

try:  # v6 标准赛制引擎（缺失时降级，不影响旧接口）
    from game import tournament as _tournament_mod
except Exception as exc:  # noqa: BLE001
    _tournament_mod = None
    _IMPORT_ERRORS["game.tournament"] = "%s: %s" % (exc.__class__.__name__, exc)

try:  # v6 赛制规则（独立 JSON，可热改）
    from game import kpl_rules as _kpl_rules_mod
except Exception as exc:  # noqa: BLE001
    _kpl_rules_mod = None
    _IMPORT_ERRORS["game.kpl_rules"] = "%s: %s" % (exc.__class__.__name__, exc)


def _import_member(module_name, attr=None):
    """import game.<module_name>，失败返回 None 并把原因记进 _IMPORT_ERRORS。"""
    label = "game.%s" % module_name
    try:
        module = __import__("game.%s" % module_name, fromlist=["*"])
    except Exception as exc:
        _IMPORT_ERRORS[label] = "%s: %s" % (exc.__class__.__name__, exc)
        return None
    if attr is None:
        return module
    target = getattr(module, attr, None)
    if target is None:
        _IMPORT_ERRORS[label] = "%s 里没有 %s" % (label, attr)
    return target


data_loader = _import_member("data_loader")
bp_engine = _import_member("bp_engine")
scorer = _import_member("scorer")
scoring_rules = _import_member("scoring_rules")
ai_coach = _import_member("ai_coach")
career_engine = _import_member("career_engine")
ai_config = _import_member("ai_config")
llm_coach = _import_member("llm_coach")
room_engine = _import_member("room_engine")
room_server = _import_member("room_server")
resource_manager = _import_member("resource_manager")
server_config = _import_member("server_config")  # v9 补丁：监听 IP / 端口可配置


def engine_ready() -> bool:
    return bp_engine is not None and data_loader is not None


def engine_error_message() -> str:
    if not _IMPORT_ERRORS and engine_ready():
        return ""
    missing = []
    if data_loader is None:
        missing.append("data_loader")
    if bp_engine is None:
        missing.append("bp_engine")
    if scorer is None:
        missing.append("scorer")
    detail = "；".join("%s（%s）" % (name, _IMPORT_ERRORS.get("game.%s" % name, "未交付"))
                      for name in missing)
    return "游戏引擎未就绪：缺少 %s" % detail


class APIError(Exception):
    """带 HTTP 状态码的业务异常，统一转成 {"ok": false, "error": ...}。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = int(status)


# --------------------------------------------------------------------------- #
# JSON 安全化
# --------------------------------------------------------------------------- #

def _safe(obj):
    """转成 JSON 安全结构：NaN/Inf -> None，datetime -> str，未知对象 -> str。"""
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            out[key if isinstance(key, (str, int)) else str(key)] = _safe(value)
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_safe(item) for item in obj]
    try:
        return str(obj)
    except Exception:
        return None


def json_response(payload, status=200):
    body = json.dumps(_safe(payload), ensure_ascii=False, allow_nan=False)
    return Response(body, status=status, mimetype="application/json")


def ok(payload=None, status=200):
    data = {"ok": True}
    if payload:
        data.update(payload)
    return json_response(data, status)


def fail(message, status=400):
    """构造一个 {"ok": false, "error": ...} 响应对象。

    v48：以前它只是 return，调用方若写成 `fail(...)` 而不 return，就会**继续往下执行**，
    于是「删不存在的存档」也返回成功、「赛制引擎未就绪」也照样跑 —— 这类假成功很难查。
    现在改成直接抛 APIError，由统一错误处理器转成同样的 JSON（写成 `return fail(...)`
    的地方抛异常同样会落到错误处理器，行为一致且更安全）。
    """
    raise APIError(str(message), status)


# --------------------------------------------------------------------------- #
# 基础类型工具
# --------------------------------------------------------------------------- #

def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _as_list(value):
    return value if isinstance(value, list) else []


def _int_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def _num_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num if math.isfinite(num) else None
    if isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError:
            return None
        return num if math.isfinite(num) else None
    return None


def _first(*args):
    """`_first(d1, d2, ..., "key")` —— 按顺序取第一个非 None 的值。

    用于「优先用便捷函数返回的行、缺失时回落到原始 stats 记录」这类两级兜底。
    """
    if not args:
        return None
    key = args[-1]
    for source in args[:-1]:
        if isinstance(source, dict):
            value = source.get(key)
            if value is not None:
                return value
    return None


# --------------------------------------------------------------------------- #
# 数据访问：只走 data_loader.load_*
#
# 契约 §1 冻结了 data/*.json 的字段；DATA_DIR 由 data_loader 统一解析，
# 因此这里绝不手拼路径。data_loader 不可用时返回 None -> 上层降级为空态。
# --------------------------------------------------------------------------- #

def dl(name, *args, **kwargs):
    """安全调用 data_loader 上的函数；不存在或抛异常时返回 None。"""
    if data_loader is None:
        return None
    fn = getattr(data_loader, name, None)
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _leagues_payload():
    """返回 (leagues_list, default_league_id)。

    优先 `data_loader.leagues_payload()`（engine 侧已验证的便捷函数），
    退化到 `load_leagues()` 自行组装。
    """
    raw = _as_dict(dl("leagues_payload"))
    if not raw:
        raw = _as_dict(dl("load_leagues"))
    leagues = [lg for lg in _as_list(raw.get("leagues"))
               if isinstance(lg, dict) and lg.get("league_id")]

    default_id = raw.get("default_league_id")
    if not default_id:
        default_id = dl("get_default_league_id")
    if not default_id and leagues:
        ended = [lg for lg in leagues if lg.get("status") == "ended"]
        default_id = (ended or leagues)[-1].get("league_id")
    if default_id is not None:
        default_id = str(default_id)
    return leagues, default_id


def _find_league(league_id):
    if league_id in (None, ""):
        return None
    leagues, _ = _leagues_payload()
    for lg in leagues:
        if str(lg.get("league_id")) == str(league_id):
            return lg
    return None


def resolve_league_id(raw_league_id):
    """校验 league_id；省略时用 default_league_id。

    leagues 数据尚未抓取（空表）时不 404，让接口返回空态而非 500。
    """
    leagues, default_id = _leagues_payload()

    if isinstance(raw_league_id, str):
        league_id = raw_league_id.strip()
    else:
        league_id = raw_league_id
    if league_id == "":
        league_id = None

    if not leagues:
        return str(league_id) if league_id is not None else default_id

    if league_id is None:
        return default_id

    league_id = str(league_id)
    if _find_league(league_id) is None:
        available = "、".join(str(lg.get("league_id")) for lg in leagues)
        raise APIError("未知的 league_id：%s（可用：%s）" % (league_id, available), 404)
    return league_id


# --------------------------------------------------------------------------- #
# 单个赛季的只读数据视图
# --------------------------------------------------------------------------- #

_CTX_HEROES = ("heroes", "hero_list")
_CTX_HERO_STATS = ("heroes", "hero_stats", "stats")
_CTX_TEAMS = ("teams", "team_stats")
_CTX_PLAYERS = ("players", "player_stats")
_CTX_MATCHES = ("matches", "bp_matches")


class LeagueResources:
    """把 data_loader 的 load_* 结果归一化成前端直接可用的行。

    每个 load_* 的返回形态做宽松归一（允许 list 或 {key: list} 包裹），
    但字段名严格按契约 §1/§2，绝不改名、绝不硬编码数值。
    """

    def __init__(self, league_id):
        self.league_id = league_id
        self._heroes = None
        self._hero_stats = None
        self._team_stats = None
        self._team_meta = None
        self._players = None
        self._bp = None
        self._bp_raw = None
        self._hero_stats_raw = None
        self._hydrated = False

    # -- 原始层 ----------------------------------------------------------
    def _load_heroes_base(self):
        raw = dl("load_heroes")
        if isinstance(raw, dict):
            raw = raw.get("heroes")
        items = _as_list(raw)
        return [h for h in items if isinstance(h, dict) and h.get("ename") is not None]

    def _load_team_meta_map(self):
        raw = dl("load_team_meta")
        mapping = {}
        if isinstance(raw, dict):
            items = raw.get("teams")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        mapping[str(item.get("team_id"))] = item
            elif "team_id" in raw:
                mapping[str(raw.get("team_id"))] = raw
            else:
                for key, item in raw.items():
                    if isinstance(item, dict):
                        mapping[str(key)] = item
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("team_id") is not None:
                    mapping[str(item.get("team_id"))] = item
        return mapping

    def _load_hero_stats(self):
        raw = _as_dict(dl("load_hero_stats", self.league_id))
        items = _as_list(raw.get("heroes"))
        mapping = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            ename = _int_or_none(item.get("ename"))
            if ename is not None:
                mapping[ename] = item
        return raw, mapping

    def _load_team_stats(self):
        raw = _as_dict(dl("load_team_stats", self.league_id))
        items = [t for t in _as_list(raw.get("teams")) if isinstance(t, dict)]
        return items

    def _load_players(self):
        raw = _as_dict(dl("load_player_stats", self.league_id))
        return [p for p in _as_list(raw.get("players")) if isinstance(p, dict)]

    def _load_bp(self):
        raw = _as_dict(dl("load_bp_history", self.league_id))
        items = [m for m in _as_list(raw.get("matches")) if isinstance(m, dict)]
        return raw, items

    def _hydrate(self):
        if self._hydrated:
            return
        self._heroes = self._load_heroes_base()
        self._hero_stats_raw, self._hero_stats = self._load_hero_stats()
        self._team_stats = self._load_team_stats()
        self._team_meta = self._load_team_meta_map()
        self._players = self._load_players()
        self._bp_raw, self._bp = self._load_bp()
        self._hydrated = True

    # -- 对外视图 --------------------------------------------------------
    @property
    def heroes(self):
        self._hydrate()
        return self._heroes or []

    @property
    def team_stats(self):
        self._hydrate()
        return self._team_stats or []

    @property
    def players(self):
        self._hydrate()
        return self._players or []

    @property
    def bp_matches(self):
        self._hydrate()
        return self._bp or []

    def hero_view(self):
        """契约 §2 /api/heroes：基础表左连接赛季数据。

        优先用 `data_loader.hero_list(league_id)`（engine 侧已实现 §2 排序），
        不可用时本文件按同一规则自建，绝不由 API 层硬编码任何数值。
        """
        self._hydrate()

        fast = dl("hero_list", self.league_id) if self.league_id else None
        rows = self._normalize_hero_rows(fast)
        if rows is None or (not rows and self.heroes):
            # 便捷函数不可用、或返回空但基础表有英雄时，自建同形状（空态不丢基础信息）
            rows = self._normalize_hero_rows(None, build=True)
        rows.sort(key=_hero_sort_key)
        return rows

    def _normalize_hero_rows(self, source, build=False):
        """把 hero_list 结果或基础表+stats 归一成契约 §2 的行。"""
        stats = self._hero_stats or {}

        if source is not None:
            items = _as_list(source)
            if isinstance(source, dict):
                items = _as_list(source.get("heroes"))
            rows = []
            for item in items:
                if not isinstance(item, dict):
                    return None
                ename = _int_or_none(item.get("ename"))
                if ename is None:
                    return None
                role = item.get("role")
                if not isinstance(role, list):
                    role = [role] if isinstance(role, str) and role else []
                # hero_list() 只给契约 §2 的字段；英雄详情弹层还要
                # win_count/game_count/avg_kill/avg_death/avg_assist，
                # 这些在 hero_stats 原始记录里，按 ename 补齐（有就带、没有就 null）。
                stat = stats.get(ename) or {}
                # 契约 v2 §2.1：lane 四件套只用该赛季数据（hero_list 行 / 赛季 stats）
                lane_fields = _lane_fields(item, stat)
                rows.append({
                    "ename": ename,
                    "cname": item.get("cname"),
                    "title": item.get("title"),
                    "lane": lane_fields["lane"],
                    "role": role,
                    "lane_list": lane_fields["lane_list"],
                    "lane_games": lane_fields["lane_games"],
                    "lane_source": lane_fields["lane_source"],
                    "lane_official": item.get("lane_official") or [],
                    "lane_missing": lane_fields["lane_missing"],
                    "avatar_path": item.get("avatar_path"),
                    "avatar_ok": bool(item.get("avatar_ok")),
                    "win_rate": _num_or_none(item.get("win_rate")),
                    "pick_rate": _num_or_none(item.get("pick_rate")),
                    "ban_rate": _num_or_none(item.get("ban_rate")),
                    "pick_count": _int_or_none(item.get("pick_count")),
                    "ban_count": _int_or_none(item.get("ban_count")),
                    "win_count": _int_or_none(_first(item, stat, "win_count")),
                    "game_count": _int_or_none(_first(item, stat, "game_count")),
                    "kda": _num_or_none(item.get("kda")),
                    "avg_kill": _num_or_none(_first(item, stat, "avg_kill")),
                    "avg_death": _num_or_none(_first(item, stat, "avg_death")),
                    "avg_assist": _num_or_none(_first(item, stat, "avg_assist")),
                    "has_stats": bool(item.get("has_stats")),
                })
            return rows

        if not build:
            return None

        rows = []
        for hero in self.heroes:
            ename = _int_or_none(hero.get("ename"))
            if ename is None:
                continue
            stat = stats.get(ename) or {}

            role = hero.get("role")
            if not isinstance(role, list):
                role = [role] if isinstance(role, str) and role else []
            lane_fields = _lane_fields(None, stat)

            win_rate = _num_or_none(stat.get("win_rate"))
            pick_rate = _num_or_none(stat.get("pick_rate"))
            ban_rate = _num_or_none(stat.get("ban_rate"))
            pick_count = _int_or_none(stat.get("pick_count"))
            ban_count = _int_or_none(stat.get("ban_count"))
            win_count = _int_or_none(stat.get("win_count"))
            game_count = _int_or_none(stat.get("game_count"))
            kda = _num_or_none(stat.get("kda"))
            avg_kill = _num_or_none(stat.get("avg_kill"))
            avg_death = _num_or_none(stat.get("avg_death"))
            avg_assist = _num_or_none(stat.get("avg_assist"))
            has_stats = any(v is not None for v in
                            (win_rate, pick_rate, ban_rate, pick_count, ban_count,
                             win_count, game_count, kda,
                             avg_kill, avg_death, avg_assist))

            rows.append({
                "ename": ename,
                "cname": hero.get("cname"),
                "title": hero.get("title"),
                "lane": lane_fields["lane"],
                "role": role,
                "lane_list": lane_fields["lane_list"],
                "lane_games": lane_fields["lane_games"],
                "lane_source": lane_fields["lane_source"],
                "lane_missing": lane_fields["lane_missing"],
                "avatar_path": hero.get("avatar_path"),
                "avatar_ok": bool(hero.get("avatar_ok")),
                "win_rate": win_rate,
                "pick_rate": pick_rate,
                "ban_rate": ban_rate,
                "pick_count": pick_count,
                "ban_count": ban_count,
                "win_count": win_count,
                "game_count": game_count,
                "kda": kda,
                "avg_kill": avg_kill,
                "avg_death": avg_death,
                "avg_assist": avg_assist,
                "has_stats": has_stats,
            })
        return rows

    def hero_name_map(self):
        return {row["ename"]: row.get("cname") for row in self.hero_view()}

    def team_view(self):
        """契约 §2 /api/teams：team_stats 左连接 team_meta。

        logo/abbreviation 优先取 `data_loader.team_objects()`（引擎已合并
        team_stats + team_meta），不可用时用 team_meta 兜底。
        """
        self._hydrate()
        objects = _as_dict(dl("team_objects", self.league_id)) if self.league_id else {}
        rows = []
        for team in self.team_stats:
            team_id = team.get("team_id")
            name = team.get("name")
            meta = self._team_meta.get(str(team_id)) if team_id is not None else None
            meta = meta or {}
            obj = _as_dict(objects.get(name)) if name else {}

            rows.append({
                "team_id": str(team_id) if team_id is not None else None,
                "name": name or meta.get("name") or obj.get("name"),
                "abbreviation": (team.get("abbreviation")
                                 or obj.get("abbreviation")
                                 or meta.get("abbreviation")),
                "logo_path": obj.get("logo_path") or meta.get("logo_path"),
                "matches": _int_or_none(team.get("matches")),
                "wins": _int_or_none(team.get("wins")),
                "losses": _int_or_none(team.get("losses")),
                "game_diff": _int_or_none(team.get("game_diff")),
                "win_rate": _num_or_none(team.get("win_rate")),
                "blue_win_rate": _num_or_none(team.get("blue_win_rate")),
                "red_win_rate": _num_or_none(team.get("red_win_rate")),
                "avg_kda": _num_or_none(team.get("avg_kda")),
                "avg_kill": _num_or_none(team.get("avg_kill")),
                "avg_death": _num_or_none(team.get("avg_death")),
                "rank": _int_or_none(team.get("rank")),
            })
        rows.sort(key=_team_sort_key)
        return rows

    def team_names(self):
        names = []
        for row in self.team_view():
            name = row.get("name")
            if name and name not in names:
                names.append(name)
        return names

    def team_brief(self, name):
        """契约 §3 的 blue_team / red_team 对象。"""
        for row in self.team_view():
            if row.get("name") == name:
                return {
                    "name": row.get("name"),
                    "logo_path": row.get("logo_path"),
                    "abbreviation": row.get("abbreviation"),
                }
        return {"name": name, "logo_path": None, "abbreviation": None}

    def player_view(self):
        rows = []
        for player in self.players:
            team_id = player.get("team_id")
            rows.append({
                "name": player.get("name"),
                "team": player.get("team"),
                "team_id": str(team_id) if team_id is not None else None,
                "position": player.get("position"),
                "matches": _int_or_none(player.get("matches")),
                "wins": _int_or_none(player.get("wins")),
                "losses": _int_or_none(player.get("losses")),
                "win_rate": _num_or_none(player.get("win_rate")),
                "kda": _num_or_none(player.get("kda")),
                "gpm": _num_or_none(player.get("gpm")),
                "gold_rate": _num_or_none(player.get("gold_rate")),
                "damage_share": _num_or_none(player.get("damage_share")),
                "participation_rate": _num_or_none(player.get("participation_rate")),
            })
        return rows

    def bp_view(self, limit):
        """契约 §2 /api/bp/history —— step_count 用真实值（不是 18）。"""
        rows = []
        for item in self.bp_matches:
            sequence = _as_list(item.get("sequence"))
            rows.append({
                "match_id": item.get("match_id"),
                "battle_id": item.get("battle_id"),
                "battle_seq": _int_or_none(item.get("battle_seq")),
                "blue_team": item.get("blue_team"),
                "red_team": item.get("red_team"),
                "winner": item.get("winner"),
                "bo": _int_or_none(item.get("bo")),
                "stage": item.get("stage"),
                "start_time": item.get("start_time"),
                "step_count": _int_or_none(item.get("step_count")) or len(sequence),
                "sequence": sequence,
                "blue_picks": _as_list(item.get("blue_picks")),
                "red_picks": _as_list(item.get("red_picks")),
                "blue_bans": _as_list(item.get("blue_bans")),
                "red_bans": _as_list(item.get("red_bans")),
            })
        total = len(rows)
        return total, rows[:limit]

    def real_bp_step_count(self):
        self._hydrate()
        value = _int_or_none(_as_dict(self._bp_raw).get("_step_count"))
        if value:
            return value
        counts = {}
        for item in self.bp_matches:
            length = len(_as_list(item.get("sequence")))
            if length:
                counts[length] = counts.get(length, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

    def has_bp_history(self):
        self._hydrate()
        if bool(_as_dict(self._bp_raw).get("_missing")):
            return False
        return len(self.bp_matches) > 0

    def league_name(self):
        self._hydrate()
        for raw in (self._bp_raw, self._hero_stats_raw):
            name = _as_dict(raw).get("league_name")
            if name:
                return name
        return None

    def fetched_at(self):
        self._hydrate()
        for raw in (self._bp_raw, self._hero_stats_raw):
            value = _as_dict(raw).get("_fetched_at")
            if value:
                return value
        return _as_dict(dl("load_leagues")).get("_fetched_at")


def _hero_sort_key(row):
    """has_stats 优先 → win_rate 降序 → ename 升序。"""
    win_rate = row.get("win_rate")
    return (
        0 if row.get("has_stats") else 1,
        0 if isinstance(win_rate, (int, float)) else 1,
        -(win_rate if isinstance(win_rate, (int, float)) else 0.0),
        row.get("ename") if isinstance(row.get("ename"), int) else 10 ** 9,
    )

def _lane_text(value):
    """单个分路值 -> 干净的字符串（空/非字符串 -> None）。"""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _lane_list_of(*sources):
    """契约 v2 §2.1：取 lane_list（去重、保持顺序、剔除空值）。

    `sources` 是按优先级排列的 dict，遇到第一个含**非空** `lane_list` 的就用；
    全都没有时返回 `[]`（绝不回退到 hero_type/role —— 契约 §5 明令禁止）。
    """
    for source in sources:
        if not isinstance(source, dict):
            continue
        raw = source.get("lane_list")
        if raw is None:
            continue
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            continue
        lanes, seen = [], set()
        for item in raw:
            text = _lane_text(item)
            if text and text not in seen:
                seen.add(text)
                lanes.append(text)
        if lanes:
            return lanes
    return []


def _lane_games_of(*sources):
    """契约 v2 §2.1：取 lane_games（{分路: 局数}），缺失返回 {}。"""
    for source in sources:
        if not isinstance(source, dict):
            continue
        raw = source.get("lane_games")
        if not isinstance(raw, dict):
            continue
        games = {}
        for key, value in raw.items():
            text = _lane_text(key)
            count = _int_or_none(value)
            if text and count is not None:
                games[text] = count
        if games:
            return games
    return {}


def _lane_fields(item, stat):
    """把 lane_list / lane_games / lane_source / lane_missing 归一成契约 v2 §2.1 的四键。

    只接受**该赛季**的两个来源：`data_loader.hero_list()` 的行 → 赛季 hero_stats 原始记录。
    绝不回退到 `heroes.json` 的全量 lane_list、role 或 hero_type
    （契约 v2 §5：禁止用其它赛季/全量数据填充该赛季的 lane_list）。
    因此该赛季无分路数据时 `lane_list=[]`、`lane_games={}`、`lane_missing=true`、`lane=null`。
    """
    lanes = _lane_list_of(item, stat)
    games = _lane_games_of(item, stat)
    source = None
    for candidate in (item, stat):
        if isinstance(candidate, dict):
            text = _lane_text(candidate.get("lane_source"))
            if text:
                source = text
                break
    return {
        "lane": lanes[0] if lanes else None,
        "lane_list": lanes,
        "lane_games": games,
        "lane_source": source or LANE_SOURCE,
        # lane_list 为空 => 该赛季没有该英雄的分路数据（前端显示「暂无该赛季分路数据」）
        "lane_missing": not lanes,
    }


def _team_sort_key(row):
    rank = row.get("rank")
    win_rate = row.get("win_rate")
    return (
        0 if isinstance(rank, int) else 1,
        rank if isinstance(rank, int) else 10 ** 9,
        -(win_rate if isinstance(win_rate, (int, float)) else 0.0),
        str(row.get("name") or ""),
    )


def league_resources(league_id):
    return LeagueResources(league_id)


# --------------------------------------------------------------------------- #
# match 内存态仓库（多线程安全 + 30 分钟滑动 TTL）
# --------------------------------------------------------------------------- #

MATCHES = {}
_MATCH_LOCK = threading.RLock()
_last_sweep = 0.0


def new_match_id():
    """m_ + 8 位随机 hex（4 字节）。"""
    while True:
        mid = "m_" + secrets.token_hex(4)
        with _MATCH_LOCK:
            if mid not in MATCHES:
                return mid


def _sweep_locked(now):
    dead = [mid for mid, entry in MATCHES.items()
            if (now - float(entry.get("touched", 0.0))) > MATCH_TTL_SECONDS]
    for mid in dead:
        MATCHES.pop(mid, None)
    return len(dead)


def store_match(match):
    """登记 / 刷新一个 match（调用方负责 match_id 与 fields 的完整性）。"""
    global _last_sweep
    if not isinstance(match, dict):
        raise APIError("游戏引擎返回了非法的 match 对象", 500)
    match_id = str(match.get("match_id") or "")
    if not match_id:
        match_id = new_match_id()
        match["match_id"] = match_id
    now = time.time()
    with _MATCH_LOCK:
        if now - _last_sweep > MATCH_SWEEP_MIN_INTERVAL:
            _sweep_locked(now)
            _last_sweep = now
        entry = MATCHES.get(match_id) or {"created": now}
        entry["match"] = match
        entry["touched"] = now
        MATCHES[match_id] = entry
        # v52：超过硬上限时淘汰最久未操作的对局，避免长时间挂机把内存吃满
        if len(MATCHES) > MATCH_MAX_COUNT:
            drop = sorted(MATCHES.items(), key=lambda kv: float(kv[1].get("touched", 0.0)))
            for mid, _entry in drop[: len(MATCHES) - MATCH_MAX_COUNT]:
                if mid != match_id:
                    MATCHES.pop(mid, None)
    return match_id


def load_match(match_id):
    """取 match 并刷新滑动 TTL；不存在 / 已过期 -> 404。"""
    global _last_sweep
    if not isinstance(match_id, str):
        raise APIError("match_id 不能为空", 400)
    match_id = match_id.strip()
    if not match_id:
        raise APIError("match_id 不能为空", 400)

    now = time.time()
    with _MATCH_LOCK:
        if now - _last_sweep > MATCH_SWEEP_MIN_INTERVAL:
            _sweep_locked(now)
            _last_sweep = now
        entry = MATCHES.get(match_id)
        if entry is None:
            raise APIError(
                "对局不存在或已过期：%s（内存态保留 %d 分钟）"
                % (match_id, MATCH_TTL_SECONDS // 60), 404)
        if (now - float(entry.get("touched", 0.0))) > MATCH_TTL_SECONDS:
            MATCHES.pop(match_id, None)
            raise APIError(
                "对局已过期（超过 %d 分钟未操作）：%s"
                % (MATCH_TTL_SECONDS // 60, match_id), 404)
        entry["touched"] = now
        return entry["match"]


def active_match_count():
    with _MATCH_LOCK:
        return len(MATCHES)


# --------------------------------------------------------------------------- #
# 请求参数
# --------------------------------------------------------------------------- #

def request_payload():
    """合并 query / form / JSON body（都允许，方便前端与测试客户端）。"""
    data = {}
    for source in (request.args, request.form):
        try:
            data.update(source.to_dict(flat=True))
        except Exception:
            pass
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        data.update(body)
    return data


def require_engine(feature="对局引擎"):
    if not engine_ready():
        raise APIError("%s不可用：%s" % (feature, engine_error_message()), 503)


def _enum_param(payload, key, allowed, default):
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    text = str(value).strip().lower()
    if text not in allowed:
        raise APIError("参数 %s 只接受 %s，收到：%s" % (key, " / ".join(allowed), text), 400)
    return text


def _int_param(payload, key, default, allowed=None, minimum=None, maximum=None):
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    parsed = _int_or_none(value)
    if parsed is None:
        raise APIError("参数 %s 必须是整数，收到：%r" % (key, value), 400)
    if allowed is not None and parsed not in allowed:
        raise APIError("参数 %s 只接受 %s，收到：%s"
                       % (key, " / ".join(str(a) for a in allowed), parsed), 400)
    if minimum is not None and parsed < minimum:
        raise APIError("参数 %s 不能小于 %s，收到：%s" % (key, minimum, parsed), 400)
    if maximum is not None and parsed > maximum:
        raise APIError("参数 %s 不能大于 %s，收到：%s" % (key, maximum, parsed), 400)
    return parsed


def _match_id_param(payload):
    value = payload.get("match_id")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise APIError("缺少 match_id", 400)
    return str(value).strip()


def _match_phase(match):
    phase = match.get("phase") if isinstance(match, dict) else None
    return str(phase) if isinstance(phase, str) else PHASE_BP


def _require_bp_phase(match, hint="请先选择阵营（POST /api/bp/side_choice）"):
    """契约 v2 §3.3：非 BP 阶段禁止触达 BP。

    `side_choice` 阶段必须被拦在这里，绝不允许穿透到引擎再报 500。
    `result` / `done` 阶段不在此抛错——调用方原有的 `finished` / `series_over`
    分支会给出更具体的提示（保持 v1 文案不变）。
    """
    phase = _match_phase(match)
    if phase in BP_PHASES:
        return
    if phase == PHASE_SIDE_CHOICE:
        raise APIError(hint, 409)
    if phase in (PHASE_RESULT, PHASE_DONE):
        return
    raise APIError("当前不在 BP 阶段（phase=%s），无法执行 BP 操作" % phase, 409)


# --------------------------------------------------------------------------- #
# 静态页路由
# --------------------------------------------------------------------------- #

_PLACEHOLDER = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KPL BP 模拟器</title>
<style>
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#FAF7F2;
color:#3d2f22;margin:0;padding:48px 20px;line-height:1.7}
.card{max-width:720px;margin:0 auto;background:#FFFDF9;border-radius:16px;padding:32px;
box-shadow:0 6px 28px rgba(255,140,66,.14)}
h1{color:#FF8C42;margin-top:0;font-size:22px}
code{background:#F3EDE4;padding:2px 6px;border-radius:5px;font-size:13px}
.hint{color:#8a7761;font-size:14px}
a{color:#FF8C42}
</style></head><body><div class="card">
<h1>页面模板未就绪</h1>
<p><code>__PAGE__</code> 还没交付到 <code>templates/</code>，当前是后端占位页。</p>
<p class="hint">API 已可用：<a href="/api/leagues">/api/leagues</a> ·
<a href="/api/league_meta">/api/league_meta</a> ·
<a href="/api/heroes">/api/heroes</a> ·
<a href="/api/teams">/api/teams</a> ·
<a href="/api/players">/api/players</a> ·
<a href="/api/bp/history">/api/bp/history</a> ·
<a href="/api/health">/api/health</a></p>
</div></body></html>"""


# --------------------------------------------------------------------------- #
# v33：头像 / 队徽从外部 images/ 目录提供（官方出新英雄时不用重编 exe）
# --------------------------------------------------------------------------- #
@app.route("/static/images/<path:rel>", methods=["GET"])
def external_image(rel):
    if ".." in rel.replace("\\", "/").split("/"):
        return ("", 404)
    try:
        base = os.path.abspath(IMAGES_DIR)
        path = os.path.abspath(os.path.join(base, *rel.split("/")))
        if path.startswith(base + os.sep) and os.path.isfile(path):
            return send_file(path, max_age=60 * 60 * 24 * 30)
    except Exception:  # noqa: BLE001
        app.logger.debug("[images] 外部图片读取失败：%s", rel, exc_info=True)
    # 回退到打包内置的静态资源
    return app.send_static_file("images/" + rel)


def _render_page(template_name):
    """渲染页面；模板未交付时返回 200 中文占位页（前端可先用 API 联调）。"""
    try:
        return render_template(template_name)
    except Exception:
        # jinja2.TemplateNotFound 不额外 import；任何渲染失败都退到占位页，
        # 因为缺模板不属于服务器错误（契约要求页面路由 200 可达）。
        return Response(_PLACEHOLDER.replace("__PAGE__", template_name),
                        status=200, mimetype="text/html")


@app.route("/", methods=["GET"])
def page_index():
    return _render_page("index.html")


@app.route("/arena", methods=["GET"])
def page_arena():
    return _render_page("arena.html")


@app.route("/data", methods=["GET"])
def page_data():
    return _render_page("data_center.html")


@app.route("/career", methods=["GET"])
def page_career():
    return _render_page("career.html")


@app.route("/saves", methods=["GET"])
def page_saves():
    """v47：存档独立成页（底部导航「存档」），不再挤在「我的」里。"""
    return _render_page("saves.html")


# --------------------------------------------------------------------------- #
# GET /api/leagues
# --------------------------------------------------------------------------- #

@app.route("/api/leagues", methods=["GET"])
def api_leagues():
    leagues, default_id = _leagues_payload()
    rows = []
    for lg in leagues:
        rows.append({
            "league_id": str(lg.get("league_id")),
            "name": lg.get("name"),
            "status": lg.get("status"),
            "start": lg.get("start"),
            "end": lg.get("end"),
            "match_count": _int_or_none(lg.get("match_count")),
            "year": _int_or_none(lg.get("year")),
            "season": _int_or_none(lg.get("season")),
        })
    return ok({"default_league_id": default_id, "leagues": rows})


# --------------------------------------------------------------------------- #
# GET /api/league_meta
# --------------------------------------------------------------------------- #

@app.route("/api/league_meta", methods=["GET"])
def api_league_meta():
    league_id = resolve_league_id(request.args.get("league_id"))
    league = _find_league(league_id) or {}
    res = league_resources(league_id)
    # 引擎侧的 league_meta 只用来补本层拿不到的元信息，counts 一律由本层按
    # 同一份 load_* 数据现算，保证与 /api/heroes 等接口的数字自洽。
    engine_meta = _as_dict(dl("league_meta", league_id)) if league_id else {}

    heroes = res.heroes
    heroes_with_stats = sum(1 for row in res.hero_view() if row.get("has_stats"))

    return ok({
        "league_id": league_id,
        "league_name": (league.get("name") or engine_meta.get("league_name")
                        or res.league_name()),
        "status": league.get("status") or engine_meta.get("status"),
        "start": league.get("start") or engine_meta.get("start"),
        "end": league.get("end") or engine_meta.get("end"),
        "counts": {
            "heroes": len(heroes),
            "heroes_with_stats": heroes_with_stats,
            "teams": len(res.team_view()),
            "players": len(res.player_view()),
            "bp_matches": len(res.bp_matches),
        },
        "has_bp_history": res.has_bp_history(),
        "real_bp_step_count": res.real_bp_step_count(),
        "_fetched_at": res.fetched_at() or engine_meta.get("_fetched_at"),
    })


# --------------------------------------------------------------------------- #
# GET /api/heroes / /api/teams / /api/players
# --------------------------------------------------------------------------- #

@app.route("/api/heroes", methods=["GET"])
def api_heroes():
    league_id = resolve_league_id(request.args.get("league_id"))
    rows = league_resources(league_id).hero_view()
    return ok({"league_id": league_id, "total": len(rows), "heroes": rows})


@app.route("/api/hero_relations", methods=["GET"])
def api_hero_relations():
    """官方对位克制数据（王者荣耀官网英雄详情页「英雄关系」）。

    只读、全局（不随赛季变化）；前端英雄详情弹层用它显示「压制 / 被压制」。
    """
    if scorer is None:
        raise APIError("评分模块未就绪", 503)
    try:
        mapping = scorer.counter_map()
    except Exception as exc:                                # noqa: BLE001
        raise APIError("读取克制数据失败：%s" % exc, 500) from exc
    rows = []
    for ename, row in sorted(mapping.items()):
        rows.append({
            "ename": ename,
            "cname": row.get("cname"),
            "counters": sorted(row.get("counters") or []),
            "countered_by": sorted(row.get("countered_by") or []),
        })
    return ok({
        "total": len(rows),
        "available": bool(rows),
        "source": "pvp.qq.com/web201605/herodetail",
        "heroes": rows,
    })


@app.route("/api/teams", methods=["GET"])
def api_teams():
    league_id = resolve_league_id(request.args.get("league_id"))
    rows = league_resources(league_id).team_view()
    return ok({"league_id": league_id, "total": len(rows), "teams": rows})


@app.route("/api/players", methods=["GET"])
def api_players():
    league_id = resolve_league_id(request.args.get("league_id"))
    rows = league_resources(league_id).player_view()
    return ok({"league_id": league_id, "total": len(rows), "players": rows})


# --------------------------------------------------------------------------- #
# GET /api/bp/history
# --------------------------------------------------------------------------- #

@app.route("/api/bp/history", methods=["GET"])
def api_bp_history():
    league_id = resolve_league_id(request.args.get("league_id"))
    limit = _int_param(request.args, "limit", 20, minimum=1, maximum=500)
    total, rows = league_resources(league_id).bp_view(limit)
    return ok({"league_id": league_id, "total": total, "limit": limit, "matches": rows})


# --------------------------------------------------------------------------- #
# match 对象装配（契约 §3）
# --------------------------------------------------------------------------- #

_MATCH_DEFAULTS = {
    "league_id": None,
    "league_name": None,
    "mode": "ai",
    "difficulty": "pro",
    "bo": 7,
    "player_side": "blue",
    "score": {"blue": 0, "red": 0},
    "game_index": 1,
    "phase": "bp",
    "step": 1,
    "action": "ban",
    "side": "blue",
    "total_steps": 20,   # v11：每方 5 ban（先 2 后 3），共 20 步
    "turn_label": "",
    "bans": {"blue": [], "red": []},
    "picks": {"blue": [], "red": []},
    "used_global": {"blue": [], "red": []},
    "hero_board": [],
    "available": [],
    "history": [],
    "ai_moves": [],
    "finished": False,
    "result": None,
    "final": None,
    "is_tiebreak": False,
    # ---- 契约 v2 §3.2 新增（只在缺失时补，绝不覆盖引擎值）----
    # free 模式这两个为 null，由引擎决定，这里只保证 key 存在
    "player_team": None,
    "ai_team": None,
    "series_games": [],
    "side_choice": {
        "required": False,
        "reason": None,
        "chooser_team": None,
        "chooser_role": None,
        "chosen_side": None,
        "resolved": False,
        "note": None,
    },
    "side_assignment": None,
}


def normalize_match(match):
    """只在字段**缺失**时补齐契约 §3 的形状；不覆盖引擎已有的值（含 None）。"""
    if not isinstance(match, dict):
        return match
    for key, value in _MATCH_DEFAULTS.items():
        if key in match:
            continue
        match[key] = json.loads(json.dumps(value)) if isinstance(value, (dict, list)) else value

    for key in ("bans", "picks", "used_global"):
        if not isinstance(match.get(key), dict):
            match[key] = {"blue": [], "red": []}
        else:
            for side in ("blue", "red"):
                if not isinstance(match[key].get(side), list):
                    match[key][side] = []
    for key in ("history", "ai_moves", "available", "series_games"):
        if not isinstance(match.get(key), list):
            match[key] = []
    # 契约 v2 §3.2：side_choice 是嵌套对象，引擎可能只给了部分键；
    # 只补缺失的键，绝不覆盖引擎已有的值（含 None）。
    choice = match.get("side_choice")
    if not isinstance(choice, dict):
        choice = {}
        match["side_choice"] = choice
    for key, value in _MATCH_DEFAULTS["side_choice"].items():
        if key not in choice:
            choice[key] = value
    if not isinstance(match.get("score"), dict):
        match["score"] = {"blue": 0, "red": 0}
    else:
        for side in ("blue", "red"):
            if _int_or_none(match["score"].get(side)) is None:
                match["score"][side] = 0
    if not match.get("match_id"):
        match["match_id"] = new_match_id()
    return match


def match_payload(match):
    return ok({"match": match})


def _adopt_engine_result(match, result):
    """适配引擎「返回新对象 / 原地修改 / 返回 None」三种写法，并把状态写回内存。"""
    if isinstance(result, dict) and result is not match:
        merged = normalize_match(result)
        match.clear()
        match.update(merged)
    else:
        normalize_match(match)
    store_match(match)
    return match


def _auto_complete(match):
    """v17：一键 BP。引擎若提供 ``auto_complete`` 就用它，否则退化为连续 auto_advance。"""
    fn = getattr(bp_engine, "auto_complete", None)
    if callable(fn):
        return fn(match)
    result = match
    for _ in range(64):
        result = auto_advance(result)
        if str(result.get("phase") or "") != "bp" or result.get("finished"):
            break
    return result


def _engine_exception(exc, what):
    error_cls = getattr(bp_engine, "BPError", None) if bp_engine else None
    if error_cls is not None and isinstance(exc, error_cls):
        return APIError(str(exc), 409)
    return APIError("%s失败：%s: %s" % (what, exc.__class__.__name__, exc), 500)


# --------------------------------------------------------------------------- #
# 契约 v2 §3.3：败方选边（AI 侧自动决策）
# --------------------------------------------------------------------------- #

# AI「思考」耗时（契约 v2 §3.3）。测试可用 KPL_AI_SLEEP=0 关掉，真实运行保持拟人节奏。
def _ai_sleep():
    """AI 思考停顿（v38：0.05~0.12s；历史上是 0.8~1.5s → 0.25~0.5s，玩家体感是「点完卡住」）。"""
    raw = os.environ.get("KPL_AI_SLEEP")
    if raw is not None:
        try:
            delay = float(raw)
        except ValueError:
            delay = 0.0
        if delay <= 0:
            return
    else:
        delay = random.uniform(0.05, 0.12)
    time.sleep(delay)


def _choice_dict(match):
    choice = match.get("side_choice") if isinstance(match, dict) else None
    return choice if isinstance(choice, dict) else {}


def _is_ai_chooser(match):
    """选边方是不是 AI（契约 v2 §3.2：chooser_role 为 ai；free 模式永远是人类 human）。"""
    role = _choice_dict(match).get("chooser_role")
    if isinstance(role, str):
        role = role.strip().lower()
    if role == "ai":
        return True
    if role in ("player", "human"):
        return False
    # 引擎没给 chooser_role 时按 mode 兜底：ai 模式里只要选边方不是玩家战队就是 AI
    if match.get("mode") == "ai":
        chooser = _choice_dict(match).get("chooser_team")
        player_team = (match.get("player_team") or {}).get("name") \
            if isinstance(match.get("player_team"), dict) else None
        if chooser and player_team:
            return chooser != player_team
    return False


def _team_camp_rates(match, team_name):
    """从 `team_stats_by_league` 取该战队本赛季蓝/红胜率（契约 v2 §3.3，真实数据）。"""
    if not team_name:
        return None, None
    league_id = match.get("league_id")
    raw = _as_dict(dl("load_team_stats", league_id)) if league_id else {}
    for team in _as_list(raw.get("teams")):
        if not isinstance(team, dict):
            continue
        if team.get("name") != team_name:
            continue
        return _num_or_none(team.get("blue_win_rate")), _num_or_none(team.get("red_win_rate"))
    return None, None


def _ai_chosen_side(match):
    """契约 v2 §3.3：比较该战队本赛季蓝/红胜率，取更高的一方；都缺则默认 blue。"""
    chooser = _choice_dict(match).get("chooser_team")
    blue_rate, red_rate = _team_camp_rates(match, chooser)
    if blue_rate is None and red_rate is None:
        return "blue", "该赛季无蓝红胜率数据，默认蓝色方"
    if red_rate is not None and blue_rate is None:
        return "red", "该赛季仅有红色方胜率数据（%.2f%%）" % red_rate
    if blue_rate is not None and red_rate is None:
        return "blue", "该赛季仅有蓝色方胜率数据（%.2f%%）" % blue_rate
    if blue_rate == red_rate:
        return "blue", "蓝红胜率相同（%.2f%%），默认蓝色方" % blue_rate
    if blue_rate > red_rate:
        return "blue", "蓝色方胜率更高（%.2f%% > %.2f%%）" % (blue_rate, red_rate)
    return "red", "红色方胜率更高（%.2f%% > %.2f%%）" % (red_rate, blue_rate)


def _apply_side(match, side):
    """调引擎选边并写回内存；优先用引擎的 `choose_side`。"""
    choose = getattr(bp_engine, "choose_side", None) if bp_engine else None
    if choose is None:
        raise APIError("选边功能不可用：game/bp_engine.py 缺少 choose_side（请等待引擎就绪）", 503)
    try:
        result = choose(match, side)
    except Exception as exc:
        raise _engine_exception(exc, "切换阵营")
    return _adopt_engine_result(match, result)


def _auto_choose_side(match):
    """契约 v2 §3.3：AI 选边 → 自动决策 + 跑 auto_advance。"""
    side, note = _ai_chosen_side(match)
    _ai_sleep()
    _apply_side(match, side)

    # 记录 AI 的选择依据（引擎没写 auto_note 时由 API 层补齐）
    choice = match.get("side_choice")
    if isinstance(choice, dict):
        if not choice.get("chosen_side"):
            choice["chosen_side"] = side
        choice["resolved"] = True
        if not choice.get("auto_note"):
            choice["auto_note"] = note

    advance = getattr(bp_engine, "auto_advance", None) if bp_engine else None
    if advance is not None and match.get("mode") == "ai":
        try:
            _adopt_engine_result(match, advance(match))
        except Exception as exc:
            raise _engine_exception(exc, "AI 代走")
    return match


# --------------------------------------------------------------------------- #
# POST /api/bp/new
# --------------------------------------------------------------------------- #

@app.route("/api/bp/new", methods=["POST"])
def api_bp_new():
    payload = request_payload()

    # 先把参数校验完（400），再检查引擎可用性（503）：
    # 非法入参在任何环境下都应得到 400，而不是被依赖缺失掩盖。
    league_id = resolve_league_id(payload.get("league_id"))
    mode = _enum_param(payload, "mode", ALLOWED_MODES, "ai")
    side = _enum_param(payload, "side", ALLOWED_SIDES, "blue")
    bo = _int_param(payload, "bo", 7, allowed=ALLOWED_BO)
    difficulty = _enum_param(payload, "difficulty", ALLOWED_DIFFICULTIES, "pro")
    # v4：AI 由谁决策 —— builtin（内置 AI，默认）/ llm（外部大模型）
    ai_engine = _enum_param(payload, "ai_engine", ("builtin", "llm"), "builtin")
    if ai_engine == "llm" and not _llm_ready():
        raise APIError("请先在「设置」里配置大模型 API（%s）"
                       % "、".join(_llm_state().get("missing") or []), 400)

    require_engine("对局引擎")

    res = league_resources(league_id)
    names = res.team_names()
    blue_team, red_team = _resolve_teams(res, names, payload.get("blue_team"), payload.get("red_team"))

    try:
        match = bp_engine.new_match(league_id, blue_team, red_team, bo, mode, side, difficulty)
    except Exception as exc:
        raise _engine_exception(exc, "创建对局")

    match = normalize_match(match)

    # match_id 以本服务为准（TTL/索引由 API 层完全掌控）
    match["match_id"] = new_match_id()

    if not isinstance(match.get("blue_team"), dict) or not match["blue_team"].get("name"):
        match["blue_team"] = res.team_brief(blue_team)
    if not isinstance(match.get("red_team"), dict) or not match["red_team"].get("name"):
        match["red_team"] = res.team_brief(red_team)

    match["league_id"] = match.get("league_id") or league_id
    if not match.get("league_name"):
        league = _find_league(league_id) or {}
        match["league_name"] = league.get("name") or res.league_name()
    for key, value in (("mode", mode), ("difficulty", difficulty),
                       ("bo", bo), ("player_side", side)):
        if match.get(key) in (None, ""):
            match[key] = value
    match["ai_engine"] = ai_engine
    match["ai_moves"] = match.get("ai_moves") or []
    # v4：生涯/联赛可只让决赛对手用大模型（减少 token 消耗）
    match["llm_scope"] = str(payload.get("llm_scope") or "all")
    if not isinstance(match.get("is_tiebreak"), bool):
        match["is_tiebreak"] = False
    if not match.get("created_at"):
        match["created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    store_match(match)
    return match_payload(match)


def _resolve_teams(res, names, raw_blue, raw_red):
    """校验队伍名；不属于该赛季 team_stats 时按契约用赛季前两名兜底。

    球队数据完全缺失时**不报 503**：契约 §2 要求「缺失则用赛季前两名兜底」，
    引擎也提供了 ``pick_default_teams`` 的通用兜底（无数据时返回「蓝方/红方」）。
    数据抓取完成前对局仍应能开起来（前端显示通用队名），而不是整局不可用。
    """
    if not names:
        names = _fallback_team_names(res.league_id)
        return names[0], names[1]

    def resolve_one(raw, index):
        # v6：允许自定义战队名（玩家在生涯 / 联机 / AI 联赛里自建的队伍），
        # 只要传了非空队名就原样采用，不再强制回落到赛季前两名。
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if len(names) > index:
            return names[index]
        return names[0]

    blue = resolve_one(raw_blue, 0)
    red = resolve_one(raw_red, 1)
    if blue == red:
        other = next((n for n in names if n != blue), None)
        if other is not None:
            red = other
        else:
            blue = next((n for n in names if n != red), blue)
    return blue, red


def _fallback_team_names(league_id):
    """球队数据缺失时的兜底队名（契约 §2：缺失用赛季前两名；引擎无数据给「蓝方/红方」）。"""
    pair = None
    if bp_engine is not None:
        try:
            pair = bp_engine.pick_default_teams(league_id)
        except Exception:
            pair = None
    names = []
    if isinstance(pair, (tuple, list)):
        for item in pair:
            if isinstance(item, str) and item.strip() and item.strip() not in names:
                names.append(item.strip())
    while len(names) < 2:
        names.append("蓝方" if not names else "红方")
    return names[:2]


# --------------------------------------------------------------------------- #
# GET /api/bp/state
# --------------------------------------------------------------------------- #

@app.route("/api/bp/state", methods=["GET"])
def api_bp_state():
    match = load_match(request.args.get("match_id"))
    return match_payload(match)


# --------------------------------------------------------------------------- #
# POST /api/bp/action
# --------------------------------------------------------------------------- #

@app.route("/api/bp/action", methods=["POST"])
def api_bp_action():
    payload = request_payload()
    # 顺序：参数/存在性（400/404）优先于依赖可用性（503），
    # 否则 match_id 不存在时会被 engine 缺失掩盖成 503。
    match = load_match(_match_id_param(payload))
    require_engine("对局引擎")
    _require_bp_phase(match)

    if match.get("finished"):
        raise APIError("本小局已结束，请调用 /api/bp/next_game 进入下一小局，或 /api/bp/score 查看评分", 409)

    action = payload.get("action")
    if action is None or (isinstance(action, str) and not action.strip()):
        raise APIError("缺少 action（ban / pick）", 400)
    action = str(action).strip().lower()
    if action not in ALLOWED_ACTIONS:
        raise APIError("参数 action 只接受 ban / pick，收到：%s" % action, 400)

    expected = match.get("action")
    tiebreak_pick = (
        _match_phase(match) == PHASE_TIEBREAK_PICK or bool(match.get("is_tiebreak"))
    )
    if tiebreak_pick:
        # v20：巅峰对决是盲选，没有「轮到谁」，只接受 pick
        if action != "pick":
            raise APIError("巅峰对决是盲选，只能 pick，没有 ban", 409)
    elif expected in ALLOWED_ACTIONS and action != expected:
        raise APIError(
            "当前第 %s 步要求 %s（%s 方），收到 %s"
            % (match.get("step"), expected, match.get("side"), action), 409)

    ename = _int_or_none(payload.get("ename"))
    if ename is None:
        raise APIError("参数 ename 必须是英雄数字 ID（整数），收到：%r" % payload.get("ename"), 400)

    # 出手前先记下「是谁在操作」——apply_action 之后 match["side"] 已经是下一手了
    acting_side = match.get("side")
    acting_side_input = None
    if tiebreak_pick:
        raw_side = payload.get("side")
        if isinstance(raw_side, str) and raw_side.strip().lower() in ("blue", "red"):
            acting_side_input = raw_side.strip().lower()
            acting_side = acting_side_input
    try:
        result = bp_engine.apply_action(match, ename, action, side=acting_side_input)
    except Exception as exc:
        raise _engine_exception(exc, "执行 BP 动作")

    _adopt_engine_result(match, result)
    return match_payload(match)


# --------------------------------------------------------------------------- #
# POST /api/bp/auto_complete  （v17：一键 BP，双方剩余手全部交给内置 AI）
# --------------------------------------------------------------------------- #

@app.route("/api/bp/auto_complete", methods=["POST"])
def api_bp_auto_complete():
    payload = request_payload()
    match = load_match(_match_id_param(payload))
    require_engine("对局引擎")
    _require_bp_phase(match)
    if match.get("finished"):
        return match_payload(match)

    before_step = match.get("step")
    try:
        result = _auto_complete(match)
    except Exception as exc:
        raise _engine_exception(exc, "一键 BP")

    _adopt_engine_result(match, result)
    app.logger.info("[bp] 一键 BP match=%s 步数 %s → %s phase=%s",
                    match.get("match_id"), before_step, match.get("step"), match.get("phase"))
    return match_payload(match)


# --------------------------------------------------------------------------- #
# POST /api/bp/auto
# --------------------------------------------------------------------------- #

@app.route("/api/bp/auto", methods=["POST"])
def api_bp_auto():
    payload = request_payload()
    match = load_match(_match_id_param(payload))
    require_engine("对局引擎")
    _require_bp_phase(match)

    if match.get("finished"):
        raise APIError("本小局已结束，AI 没有可执行的动作", 409)

    before = (match.get("step"), len(match.get("ai_moves") or []))
    try:
        result = _ai_auto_advance(match)
    except Exception as exc:
        raise _engine_exception(exc, "AI 代走")

    _adopt_engine_result(match, result)
    return match_payload(match)


# --------------------------------------------------------------------------- #
# POST /api/bp/next_game
# --------------------------------------------------------------------------- #

@app.route("/api/bp/next_game", methods=["POST"])
def api_bp_next_game():
    payload = request_payload()
    match = load_match(_match_id_param(payload))
    require_engine("对局引擎")

    # 契约 v2 §3.3：done 分支必须保留且放在最前（幂等，不报错）
    if match.get("phase") == PHASE_DONE:
        raise APIError("BO%s 已经打完了，请创建新对局（POST /api/bp/new）" % match.get("bo"), 409)
    if not match.get("finished"):
        raise APIError("当前小局还没结束（phase=%s），不能进入下一小局" % match.get("phase"), 409)

    try:
        result = bp_engine.next_game(match)
    except Exception as exc:
        raise _engine_exception(exc, "进入下一小局")

    _adopt_engine_result(match, result)

    # 契约 v2 §3.3：bo>1 且选边方是 AI 时后端自动选边，走完 AI 代走；
    # 玩家选边时停在 phase="side_choice" 等前端调 /api/bp/side_choice。
    if _match_phase(match) == PHASE_SIDE_CHOICE and _is_ai_chooser(match):
        _auto_choose_side(match)

    return match_payload(match)


# --------------------------------------------------------------------------- #
# POST /api/bp/side_choice（契约 v2 §3.3 新增）
# --------------------------------------------------------------------------- #

@app.route("/api/bp/side_choice", methods=["POST"])
def api_bp_side_choice():
    payload = request_payload()
    # 与 v1 一致：存在性（404）优先于依赖可用性（503）
    match = load_match(_match_id_param(payload))
    require_engine("对局引擎")

    side = payload.get("side")
    if side is None or (isinstance(side, str) and not side.strip()):
        raise APIError("缺少 side（blue / red）", 400)
    side = str(side).strip().lower()
    if side not in ALLOWED_SIDES:
        raise APIError("参数 side 只接受 %s，收到：%s"
                       % (" / ".join(ALLOWED_SIDES), side), 400)

    phase = _match_phase(match)
    choice = _choice_dict(match)
    # 校验顺序严格按契约 §3.3：phase 在前，required 在后。
    if phase != PHASE_SIDE_CHOICE:
        message = "当前不在选边阶段（phase=%s），BP 进行中不能切换阵营" % phase
        if choice.get("required") is not True:
            # 顺带说明「本局本来就不需要选边」，避免前端把它误当成时序错误
            message += "；本局无需选边（reason=%s）" % (choice.get("reason"),)
        raise APIError(message, 409)

    if choice.get("required") is not True:
        raise APIError("本局无需选边（reason=%s）" % (choice.get("reason"),), 409)

    _apply_side(match, side)

    # 契约 v2 §3.3：选边成功后 mode=ai 照常跑 auto_advance（AI 该先手就先走）
    advance = getattr(bp_engine, "auto_advance", None) if bp_engine else None
    if advance is not None and match.get("mode") == "ai":
        try:
            _adopt_engine_result(match, advance(match))
        except Exception as exc:
            raise _engine_exception(exc, "AI 代走")

    return match_payload(match)


# --------------------------------------------------------------------------- #
# POST /api/bp/score
# --------------------------------------------------------------------------- #

_SCORE_KEYS = ("system", "counter", "version", "lane", "weakness")


def _side_score(raw):
    data = _as_dict(raw)
    breakdown_raw = _as_dict(data.get("breakdown"))
    breakdown = {}
    for key in _SCORE_KEYS:
        value = _int_or_none(breakdown_raw.get(key))
        breakdown[key] = value if value is not None else 0

    score = _int_or_none(data.get("score"))
    if score is None:
        score = sum(breakdown.values())
    score = max(0, min(100, int(score)))

    row = {
        "score": score,
        "breakdown": breakdown,
        "notes": [str(n) for n in _as_list(data.get("notes")) if str(n).strip()],
        "missing": [str(n) for n in _as_list(data.get("missing")) if str(n).strip()],
    }
    # v2：各分项当前生效上限（权重按比例转移后会变，见契约 v2 §2.3）。
    # 评分器未给出时整个键都不出现，让前端回退自己的基准上限，避免误判为「该分项无效」。
    if isinstance(data.get("caps"), dict):
        caps = {}
        for key in _SCORE_KEYS:
            value = _int_or_none(data["caps"].get(key))
            caps[key] = value if value is not None else 0
        row["caps"] = caps
    # picks 不是契约 §2 的字段；只有评分器主动给出时才透传，避免前端读到空数组误解
    if "picks" in data:
        picks = [_int_or_none(p) for p in _as_list(data.get("picks"))]
        row["picks"] = [p for p in picks if p is not None]
    return row


@app.route("/api/bp/score", methods=["POST"])
def api_bp_score():
    payload = request_payload()
    match = load_match(_match_id_param(payload))
    require_engine("评分器")
    # 契约 v2 §3.3：选边阶段还没阵容，不能评分（409，不是 500）
    if _match_phase(match) == PHASE_SIDE_CHOICE:
        raise APIError("选边阶段暂无阵容可评分", 409)
    if scorer is None:
        raise APIError("评分器不可用：game/scorer.py 未就绪", 503)
    league_id = match.get("league_id")

    ctx = dl("league_context", league_id) if league_id else None
    if ctx is None:
        raise APIError(
            "赛季上下文不可用：data_loader.league_context(%s) 返回空，无法评分" % league_id, 503)

    try:
        raw = scorer.score_match(match, ctx)
    except Exception as exc:
        raise APIError("评分失败：%s: %s" % (exc.__class__.__name__, exc), 500)

    raw = _as_dict(raw)

    # 兼容 {"blue":..,"red":..} / {"sides":{..}} / 单侧结果
    blue_raw = raw.get("blue")
    red_raw = raw.get("red")
    if blue_raw is None and red_raw is None:
        sides = _as_dict(raw.get("sides"))
        blue_raw = sides.get("blue")
        red_raw = sides.get("red")
    if blue_raw is None and red_raw is None and raw.get("breakdown") is not None:
        if (raw.get("side") or match.get("player_side")) == "red":
            red_raw = raw
        else:
            blue_raw = raw

    blue = _side_score(blue_raw)
    red = _side_score(red_raw)

    return ok({
        "match_id": match.get("match_id"),
        "blue": blue,
        "red": red,
        "delta": int(blue["score"]) - int(red["score"]),
        # recommend 归 scorer；缺失则 null（契约允许），绝不自己造候选/算分
        "recommend": _normalize_recommend(raw.get("recommend")),
    })


def _normalize_recommend(value):
    """原样透传 scorer 的 recommend，仅做形状与上限校验。"""
    data = _as_dict(value)
    if not data:
        return None
    candidates = []
    for item in _as_list(data.get("candidates"))[:5]:
        item = _as_dict(item)
        ename = _int_or_none(item.get("ename"))
        if ename is None:
            continue
        candidates.append({
            "ename": ename,
            "cname": item.get("cname"),
            "score": _num_or_none(item.get("score")),
            "reason": item.get("reason"),
        })
    if not candidates:
        return None
    return {
        "side": data.get("side"),
        "action": data.get("action"),
        "candidates": candidates,
    }


# --------------------------------------------------------------------------- #
# /api/profile —— 生涯档案（data/profile.json）
# --------------------------------------------------------------------------- #

def _profile_path():
    """profile.json 与 data_loader 使用同一个 data 目录。"""
    directory = getattr(data_loader, "DATA_DIR", None) if data_loader is not None else None
    if not directory:
        data_module = sys.modules.get("game.data_loader")
        directory = getattr(data_module, "DATA_DIR", None) if data_module else None
    if not directory:
        directory = DATA_DIR
    return os.path.join(str(directory), "profile.json")


def _empty_profile():
    return json.loads(json.dumps(DEFAULT_PROFILE))


def _read_profile():
    profile = _empty_profile()
    path = _profile_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return profile
    if not isinstance(raw, dict):
        return profile

    nickname = raw.get("nickname")
    if isinstance(nickname, str) and nickname.strip():
        profile["nickname"] = nickname.strip()[:20]
    for key in ("total_games", "wins", "losses"):
        value = _int_or_none(raw.get(key))
        profile[key] = value if value is not None and value >= 0 else 0
    usage_raw = raw.get("hero_usage")
    if isinstance(usage_raw, dict):
        usage = {}
        for key, value in usage_raw.items():
            count = _int_or_none(value)
            if count is not None and count >= 0:
                usage[str(key)] = count
        profile["hero_usage"] = usage
    last_played = raw.get("last_played")
    if isinstance(last_played, str) and last_played.strip():
        try:
            datetime.strptime(last_played.strip(), "%Y-%m-%d")
            profile["last_played"] = last_played.strip()
        except ValueError:
            profile["last_played"] = None
    return profile


def _write_profile(profile):
    path = _profile_path()
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise APIError("无法创建目录 %s：%s" % (directory, exc), 500)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(profile, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise APIError("写入 profile.json 失败：%s" % exc, 500)


@app.route("/api/profile", methods=["GET"])
def api_profile_get():
    return ok({"profile": _read_profile()})


@app.route("/api/profile", methods=["POST", "PUT"])
def api_profile_post():
    payload = request_payload()
    known = ("nickname", "total_games", "wins", "losses", "hero_usage", "last_played")
    if not any(key in payload for key in known):
        raise APIError("请求体里没有任何可写字段（可用：%s）" % " / ".join(known), 400)

    profile = _read_profile()

    if "nickname" in payload:
        nickname = payload.get("nickname")
        if not isinstance(nickname, str) or not nickname.strip():
            raise APIError("nickname 必须是非空字符串", 400)
        nickname = nickname.strip()
        if len(nickname) > 20:
            raise APIError("nickname 最多 20 个字符，收到 %d 个" % len(nickname), 400)
        profile["nickname"] = nickname

    for key in ("total_games", "wins", "losses"):
        if key in payload:
            value = _int_or_none(payload.get(key))
            if value is None or value < 0:
                raise APIError("%s 必须是非负整数，收到：%r" % (key, payload.get(key)), 400)
            profile[key] = value

    if "hero_usage" in payload:
        usage_raw = payload.get("hero_usage")
        if not isinstance(usage_raw, dict):
            raise APIError("hero_usage 必须是 {ename: 次数} 对象", 400)
        usage = {}
        for key, value in usage_raw.items():
            count = _int_or_none(value)
            if count is None or count < 0:
                raise APIError("hero_usage[%s] 必须是非负整数，收到：%r" % (key, value), 400)
            usage[str(key)] = count
        profile["hero_usage"] = usage

    if "last_played" in payload:
        last_played = payload.get("last_played")
        if last_played in (None, ""):
            profile["last_played"] = None
        else:
            text = str(last_played).strip()
            try:
                datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                raise APIError("last_played 必须是 YYYY-MM-DD，收到：%s" % text, 400)
            profile["last_played"] = text

    _write_profile(profile)
    return ok({"profile": profile})


# --------------------------------------------------------------------------- #
# GET /api/health —— 自检（数据/引擎/模板/内存态）
# --------------------------------------------------------------------------- #

# === v6 tourney engine (kpl_rules + tournament) ===
# --------------------------------------------------------------------------- #
# v6：标准 KPL 赛制（tournament 引擎）+ 赛程存档
#   - 存档文件：data/tourney/<save_id>.json（与 v3 的 data/career/ 完全隔离）
#   - 生涯 / 好友 PK 联赛 / 大模型 AI 联赛 三方共用同一套引擎
# --------------------------------------------------------------------------- #
TOURNEY_DIR = os.path.join(DATA_DIR, "tourney")
TOURNEY_VERSION = 4
_TOURNEY_LOCK = threading.RLock()
_TOURNEY_ID_RE = re.compile(r"^ts_[A-Za-z0-9]{6,32}$")
MAX_TOURNEYS = 60
TOURNEY_CATEGORY_LABEL = {"league": "联赛", "cup": "杯赛", "package": "赛程包"}
TOURNEY_TIERS = ((10, "传奇教头"), (6, "金牌教练"), (3, "名帅"), (1, "初露锋芒"), (0, "无名之辈"))


def _txt(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _tourney_tier(titles):
    for need, name in TOURNEY_TIERS:
        if titles >= need:
            return name
    return "无名之辈"


def tourney_ready():
    return _tournament_mod is not None and _kpl_rules_mod is not None


def require_tourney():
    if not tourney_ready():
        fail("赛制引擎未就绪（game/tournament.py 或 kpl_rules.py 加载失败）", 503)


def _tourney_dir(create=False):
    if create:
        try:
            os.makedirs(TOURNEY_DIR, exist_ok=True)
        except OSError as exc:
            app.logger.warning("[tourney] 创建存档目录失败：%s", exc)
    return TOURNEY_DIR


def _tourney_path(save_id):
    return os.path.join(_tourney_dir(), "%s.json" % save_id)


def _check_tourney_id(raw):
    save_id = _txt(raw)
    if not _TOURNEY_ID_RE.match(save_id):
        fail("存档 ID 不合法", 400)
    return save_id


def _load_tourney(save_id, *, missing_ok=False):
    path = _tourney_path(save_id)
    if not os.path.exists(path):
        if missing_ok:
            return None
        # 注意：fail() 只返回响应、不会抛异常，这里必须 raise，否则会继续往下读文件
        raise APIError("存档不存在：%s" % save_id, 404)
    try:
        with io.open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:                                  # noqa: BLE001
        app.logger.error("[tourney] 读取存档 %s 失败：%s", save_id, exc)
        if missing_ok:
            return None
        raise APIError("存档损坏，无法读取：%s" % save_id, 500)
    if not isinstance(data, dict) or not data.get("save_id"):
        if missing_ok:
            return None
        raise APIError("存档格式不正确：%s" % save_id, 500)
    return data


#: 上一次成功落盘的存档内容（save_id → 序列化后的字符串）。v23 用它挡掉
#: 「读一下就整份重写」的无谓磁盘 IO：年总包存档 250KB+，Windows 上还要过一遍
#: 杀毒扫描，每次点开赛季页都写一遍会明显拖慢响应。
_LAST_WRITTEN = {}


def _write_tourney(save, *, force=False):
    _tourney_dir(create=True)
    save_id = save["save_id"]
    save["version"] = TOURNEY_VERSION
    payload = json.dumps(save, ensure_ascii=False, separators=(",", ":"))
    if not force and _LAST_WRITTEN.get(save_id) == payload:
        return save                                  # 内容没变，不用再写盘
    save["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps(save, ensure_ascii=False, separators=(",", ":"))
    path = _tourney_path(save_id)
    tmp = path + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
        _LAST_WRITTEN[save_id] = payload
    except Exception as exc:                                  # noqa: BLE001
        app.logger.error("[tourney] 写存档 %s 失败：%s", save.get("save_id"), exc)
        fail("存档写入失败：%s" % exc, 500)
    app.logger.info("[tourney] 存档已保存 %s（阶段 %s）", save_id, save.get("stage"))
    return save


def _iter_tourneys():
    rows = []
    folder = _tourney_dir()
    if not os.path.isdir(folder):
        return rows
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        save_id = name[:-5]
        if not _TOURNEY_ID_RE.match(save_id):
            continue
        data = _load_tourney(save_id, missing_ok=True)
        if data:
            rows.append(data)
    rows.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return rows


def _tourney_row(save):
    try:
        summary = _tourney_save_view(save)
    except Exception as exc:                                  # noqa: BLE001
        app.logger.warning("[tourney] summary 失败（%s）：%s", save.get("save_id"), exc)
        summary = {}
    return {
        "save_id": save.get("save_id"),
        "save_name": save.get("save_name"),
        "mode": save.get("mode"),
        "template": save.get("template"),
        "category": save.get("category"),
        "category_label": TOURNEY_CATEGORY_LABEL.get(save.get("category"), "赛程"),
        "event": save.get("event") or {},
        "coach": save.get("coach") or {},
        "status": save.get("status"),
        "champion": save.get("champion"),
        "series_label": summary.get("series_label"),
        "series_index": summary.get("series_index"),
        "series_total": summary.get("series_total"),
        "stage": save.get("stage"),
        "stage_label": save.get("stage_label"),
        "stage_done": save.get("stage_done"),
        "current_day": save.get("current_day"),
        "total_days": save.get("total_days"),
        "annual_points": summary.get("annual_points") or save.get("annual_points") or {},
        "drawn": summary.get("drawn"),
        "progress": summary.get("progress") or 0,
        # v9 需求 2：赛程内部的真实比赛进度（progress 是「阶段」粒度，玩家更关心打了多少场）
        "matches_done": sum(1 for m in (save.get("schedule") or [])
                            if isinstance(m, dict) and m.get("status") == "done"),
        "matches_total": len(save.get("schedule") or []),
        "next_match": summary.get("next_match"),
        "stats": summary.get("stats") or {},
        # v9 需求 2：联机联赛存档需要房间信息（端口/真人列表）才能读档续玩
        "room": save.get("room") or {},
        "humans": save.get("humans") or [],
        "mode_label": {"career": "年总模式", "friend": "好友联机", "llm": "大模型联赛"}.get(
            save.get("mode") or "", "生涯"),
        "created_at": save.get("created_at"),
        "updated_at": save.get("updated_at"),
    }


def _tourney_teams(league_id):
    rows = _career_team_rows(league_id) or []
    if not rows:
        app.logger.warning("[tourney] 赛季 %s 战队为空，赛程将无法创建", league_id)
        return []
    out = []
    for idx, row in enumerate(rows, start=1):
        out.append({
            "name": _txt(row.get("name")),
            "team_id": row.get("team_id"),
            "logo_path": row.get("logo_path"),
            "abbreviation": row.get("abbreviation"),
            "win_rate": row.get("win_rate"),
            "blue_win_rate": row.get("blue_win_rate"),
            "red_win_rate": row.get("red_win_rate"),
            "rank": _int_or_none(row.get("rank")) or idx,
        })
    out.sort(key=lambda r: r.get("rank") or 99)
    return [r for r in out if r["name"]]


def _cup_league_id():
    """挑一个「挑战者杯」赛季的 league_id（用于年度赛程包补足 32 队）。"""
    try:
        leagues, _default = _leagues_payload()
    except Exception as exc:                                  # noqa: BLE001
        app.logger.warning("[tourney] 读取赛事列表失败：%s", exc)
        return None
    for row in leagues or []:
        if "挑战者杯" in _txt(row.get("name")):
            return _txt(row.get("league_id")) or None
    return None


def _tourney_teams_annual(league_id, cup_league_id=None):
    """v21：年度赛程包的参赛池 = 联赛赛季队伍（打头）+ 挑战者杯补足队。

    联赛段（春季赛 / 夏季赛）只取前 ``league_size`` 支队，挑战者杯段要凑满 32 支；
    两段共用同一个 participants 池，所以这里一次性把队池补到 32 队。
    联赛赛季自身的 18 支队一定排在前面（rank 优先），补位队排在后面。
    """
    base = _tourney_teams(league_id)
    if not base:
        return base
    try:
        want = int(_kpl_rules_mod.challenger_cfg().get("team_count") or 32)
    except Exception:                                         # noqa: BLE001
        want = 32
    league_size = len(base)
    if len(base) >= want:
        return base[:want]
    cup_id = _txt(cup_league_id) or _cup_league_id()
    if not cup_id or cup_id == _txt(league_id):
        app.logger.warning("[tourney] 找不到挑战者杯赛季数据，年度赛程包只能维持 %d 支队", len(base))
        return base
    extra = _tourney_teams(cup_id)
    known = {row["name"] for row in base}
    pool = list(base)
    for row in extra:
        if len(pool) >= want:
            break
        if row["name"] in known:
            continue
        item = dict(row)
        item["rank"] = league_size + len(pool) - league_size + 1
        pool.append(item)
        known.add(row["name"])
    app.logger.info("[tourney] 年度赛程包队池：联赛 %s %d 支（联赛段用这 %d 支）+ 挑战者杯补位 %d 支 = %d 支",
                    league_id, league_size, league_size, len(pool) - league_size, len(pool))
    return pool[:want]


def _tourney_pick_league(template_key, payload):
    league_id = _txt(payload.get("league_id"))
    if league_id:
        return league_id
    leagues, default_id = _leagues_payload()
    if not leagues:
        return None
    if template_key == "challenger":
        for row in leagues:
            if "挑战者杯" in (_txt(row.get("name"))):
                return _txt(row.get("league_id"))
    if template_key == "annual_finals":
        for row in leagues:
            if "年度总决赛" in (_txt(row.get("name"))):
                return _txt(row.get("league_id"))
    if template_key == "annual_package":
        for row in leagues:
            name = _txt(row.get("name"))
            if "挑战者杯" not in name and "年度总决赛" not in name:
                return _txt(row.get("league_id"))
    if league_id == "":
        for row in leagues:
            name = _txt(row.get("name"))
            if "挑战者杯" not in name and "年度总决赛" not in name:
                return _txt(row.get("league_id"))
    return default_id or _txt((leagues[0] or {}).get("league_id"))


def _payload_of():
    payload = request.form.to_dict() if request.form else {}
    body = request.get_json(silent=True) if request.is_json else None
    if isinstance(body, dict):
        payload.update(body)
    return payload


# --- v6 compat view (front-end reuse) ---
def _tourney_save_view(save):
    """在 tournament.summary() 上补 v3 页面需要的兼容字段。"""
    view = _tournament_mod.summary(save)
    plan = view.get("plan") or []
    stage_key = save.get("stage")
    try:
        stage_rules = (_tournament_mod.standings(save) or {}).get("rules") or ""
    except Exception as exc:                                  # noqa: BLE001
        app.logger.warning("[tourney] 读取阶段规则失败：%s", exc)
        stage_rules = ""
    index = 0
    for pos, row in enumerate(plan):
        if row.get("key") == stage_key:
            index = pos
        row["index"] = pos + 1
        row["rules"] = (stage_rules if row.get("key") == stage_key else "") or ""
    view["plan"] = plan
    view["stage_index"] = index + 1
    view["stage_total"] = len(plan)
    view["stage_complete"] = bool(save.get("stage_done"))
    view["progress"] = round(100.0 * (index + (1 if save.get("stage_done") else 0)) / len(plan)) \
        if plan else 0
    try:
        view["next_match"] = _tournament_mod.next_player_match(save)
    except Exception as exc:                                  # noqa: BLE001
        app.logger.warning("[tourney] next_player_match 失败：%s", exc)
        view["next_match"] = None
    view["drawn"] = bool(save.get("groups") or save.get("draw_order"))
    view["standings_kind"] = save.get("stage_kind")
    view["stage_rules"] = stage_rules
    view["annual"] = view.get("annual") or []
    # v28：赛程闸门（打完一站 / 进淘汰赛前停下来等玩家点按钮）
    view["draw_gate"] = save.get("draw_gate")
    # v29：当前站点的赛季 ID —— 年度赛程包每站用自己那一届的 BP 数据/AI
    try:
        idx = int(save.get("series_index") or 0)
        series = (save.get("series") or [{}])[idx] if save.get("series") else {}
    except Exception:                                         # noqa: BLE001
        series = {}
    view["series_league_id"] = (series or {}).get("league_id") or         (save.get("event") or {}).get("league_id")
    view["event_name"] = (save.get("event") or {}).get("name")
    return view


def _tourney_groups_view(save):
    """把存档里的 groups 统一成前端要的列表形态 [{key,name,teams:[...]}]。"""
    raw = save.get("groups")
    if isinstance(raw, dict):
        rows = []
        for key in sorted(raw.keys()):
            teams = raw.get(key) or []
            rows.append({"key": key, "name": key,
                         "teams": [(_txt(t.get("name")) if isinstance(t, dict) else _txt(t))
                                   for t in teams]})
        return rows
    if isinstance(raw, list):
        return raw
    return []


def _tourney_draw_view(save):
    """抽签顺序统一成 [{team, group}]（前端据此做逐个揭示动画）。"""
    raw = save.get("draw_order") or []
    groups = save.get("groups") or {}
    group_of = {}
    if isinstance(groups, dict):
        for key, teams in groups.items():
            # 只认真正的分组键（S/A/B/G1..G3/master/elite），跳过 cards_outcome 这类状态
            if not isinstance(key, str) or not (
                    key in ("S", "A", "B", "master", "elite") or
                    (len(key) >= 2 and key[0] in "GMC" and key[1:].isdigit())):
                continue
            for team in teams or []:
                name = _txt(team.get("name")) if isinstance(team, dict) else _txt(team)
                if name:
                    group_of[name] = key
    rows = []
    for item in raw:
        if isinstance(item, dict):
            name = _txt(item.get("team")) or _txt(item.get("name"))
            rows.append({"team": name, "group": _txt(item.get("group")) or group_of.get(name, "")})
        else:
            name = _txt(item)
            rows.append({"team": name, "group": group_of.get(name, "")})
    return rows


def _tourney_draw_result(save):
    """v28：抽签页数据。优先用闸门提交时生成的 draw_result（站内淘汰赛抽签也有），
    否则回退到旧的 draw_order + groups。"""
    result = save.get("draw_result")
    if isinstance(result, dict) and result.get("order"):
        return result
    return {
        "kind": "series",
        "title": "",
        "order": _tourney_draw_view(save),
        "groups": _tourney_groups_view(save),
        "needs_draw": True,
    }


def _tourney_state(save):
    try:
        _tournament_mod.advance(save)
    except Exception as exc:                                  # noqa: BLE001
        app.logger.warning("[tourney] 自动推进失败（%s）：%s", save.get("save_id"), exc)
    return {
        "save": _tourney_save_view(save),
        "plan": (_tourney_save_view(save).get("plan") or []),
        "schedule": _tournament_mod.schedule_view(save),
        "standings": _tournament_mod.standings(save),
        "bracket": _tournament_mod.bracket_view(save),
        "breakout_pick": (_tournament_mod._breakout_pick_view(save)
                          if save.get("breakout_pick") else None),
        "annual": _tournament_mod.annual_ranking(save),
        # v21：年度赛程包的积分列名跟着实际系列赛走（春季赛 / 挑战者杯 / 夏季赛）
        "annual_series": [{"slot": row.get("slot"), "label": row.get("label") or row.get("slot"),
                           "key": row.get("key"), "status": row.get("status")}
                          for row in (save.get("series") or []) if row.get("slot")],
        "groups": _tourney_groups_view(save),
        "draw_order": _tourney_draw_view(save),
        "draw_view": _tourney_draw_result(save),
        "rules_text": (_tournament_mod.standings(save) or {}).get("rules") or "",
        "log": save.get("log") or [],
    }


# v29：把 data/leagues.json 的真实赛事实例映射成「届次（edition）」。
# 数据源更新（例如之后抓到了 2027 春季赛）时，这里会自动多出对应选项，不需要改代码。
_EDITION_BASE_KEYWORDS = (
    ("春季赛", "spring"),
    ("夏季赛", "autumn"),
    ("夏季赛", "autumn"),
    ("挑战者杯", "challenger"),
    ("年度总决赛", "annual_finals"),
)
_EDITION_ORDER = ("spring", "challenger", "autumn", "annual_finals")


def _edition_base_of(name):
    text = _txt(name)
    for keyword, key in _EDITION_BASE_KEYWORDS:
        if keyword in text:
            return key
    return None


def _league_year(row):
    m = re.search(r"(20\d{2})", _txt((row or {}).get("name")))
    return m.group(1) if m else ""


def _tourney_editions(league_rows):
    """返回 {template_key: [edition, ...]}，年份新的在前。

    单个赛事 edition：``{edition_id, template, league_id, name, year, status, team_count, stations}``
    年度赛程包 edition：按年份把当年真实赛事串起来（官方名称 + 各自 league_id）。
    """
    by_key = {}
    for row in league_rows or []:
        key = _edition_base_of(row.get("name"))
        if not key:
            continue
        year = _league_year(row)
        by_key.setdefault(key, []).append({
            "edition_id": "%s:%s" % (key, _txt(row.get("league_id"))),
            "template": key,
            "league_id": _txt(row.get("league_id")),
            "name": _txt(row.get("name")),
            "year": year,
            "status": row.get("status"),
            "team_count": row.get("team_count") or 0,
            "stations": [],
        })
    for key in list(by_key):
        by_key[key].sort(key=lambda e: (e.get("year") or "", e.get("league_id") or ""),
                         reverse=True)

    years = sorted({e["year"] for rows in by_key.values() for e in rows if e.get("year")},
                   reverse=True)
    packages = []
    for year in years:
        picked = {}
        for key in _EDITION_ORDER:
            hits = [e for e in by_key.get(key) or [] if e.get("year") == year]
            if hits:
                picked[key] = hits[0]
        if "spring" not in picked and "autumn" not in picked:
            continue
        stations = [{"key": key, "label": picked[key]["name"],
                     "league_id": picked[key]["league_id"]}
                    for key in _EDITION_ORDER if key in picked]
        anchor = picked.get("spring") or picked.get("autumn")
        packages.append({
            "edition_id": "annual_package:%s" % year,
            "template": "annual_package",
            "league_id": anchor["league_id"],
            "name": "%s 年度赛程包" % year,
            "year": year,
            "status": anchor.get("status"),
            "team_count": _kpl_rules_mod.template_teams("annual_package"),
            "stations": stations,
            "station_names": " → ".join(st["label"] for st in stations),
        })
    by_key["annual_package"] = packages
    return by_key


def _editions_map():
    leagues, _default = _leagues_payload()
    league_rows = [{"league_id": _txt(l.get("league_id")), "name": _txt(l.get("name")),
                    "status": l.get("status"),
                    "team_count": len(_career_team_rows(_txt(l.get("league_id"))) or [])}
                   for l in leagues]
    return _tourney_editions(league_rows)


def _resolve_edition(edition_id):
    """按 edition_id 找届次；找不到返回 None。"""
    want = _txt(edition_id).strip()
    if not want:
        return None
    for rows in _editions_map().values():
        for row in rows:
            if row.get("edition_id") == want:
                return row
    return None


@app.route("/api/tourney/templates", methods=["GET"])
def api_tourney_templates():
    require_tourney()
    tpls = _kpl_rules_mod.templates()
    leagues, default_id = _leagues_payload()
    league_rows = [{"league_id": _txt(l.get("league_id")), "name": _txt(l.get("name")),
                    "status": l.get("status"),
                    "team_count": len(_career_team_rows(_txt(l.get("league_id"))) or [])}
                   for l in leagues]
    editions_map = _tourney_editions(league_rows)
    out = []
    for row in tpls:
        item = dict(row)
        item["category_label"] = TOURNEY_CATEGORY_LABEL.get(row.get("category"), "赛程")
        item["stages"] = []
        try:
            series = _tournament_mod.template_series(row.get("key"))
            if series:
                pipe = series[0].get("pipeline")
                for key, label, _kind in _tournament_mod.STAGE_DEFS.get(pipe, []):
                    bo_key = _tournament_mod.BO_KEY.get((pipe, key), "regular")
                    item["stages"].append({"key": key, "label": label,
                                           "bo": _kpl_rules_mod.bo_of(bo_key, 5)})
                item["series"] = [{"key": s.get("key"), "name": s.get("name"),
                                   "pipeline": s.get("pipeline")} for s in series]
        except Exception as exc:                              # noqa: BLE001
            app.logger.warning("[tourney] 模板 %s 展开失败：%s", row.get("key"), exc)
        item["leagues"] = league_rows
        editions = editions_map.get(row.get("key")) or []
        item["editions"] = editions
        item["default_edition_id"] = editions[0]["edition_id"] if editions else None
        out.append(item)
    return ok({"templates": out, "leagues": league_rows,
               "editions": editions_map,
               "default_league_id": default_id, "version": APP_VERSION})


@app.route("/api/tourney/rules", methods=["GET"])
def api_tourney_rules():
    require_tourney()
    try:
        tables = _kpl_rules_mod.rule_tables_for_comment()
    except Exception as exc:                                  # noqa: BLE001
        app.logger.warning("[tourney] 读取规则表失败：%s", exc)
        fail("规则表读取失败：%s" % exc, 500)
    return ok({"rules": tables, "path": _kpl_rules_mod.RULES_PATH})


def _package_category_stats(save):
    """把「年度赛程包」按站拆成 联赛 / 杯赛 两份统计（用户需求：赛程包里的联赛和杯赛
    也要记进联赛 / 杯赛记录，而不是单独算一类）。

    * 联赛站 = ``pipeline == "league_sab"``（春季赛 / 夏季赛），其余（挑战者杯 / 年度总决赛）算杯赛；
    * 场次 / 胜负按 ``save["schedule"]`` 里「我执教的战队」已结束的 BO 大场统计；
    * 冠军按每一站结束后的 ``ranking[0]`` 判定。
    """
    coach = _txt((save.get("coach") or {}).get("team_name"))
    series = save.get("series") or []
    cat_of = {}
    for i, item in enumerate(series):
        pipe = _txt(item.get("pipeline"))
        cat_of[i] = "league" if pipe == "league_sab" else "cup"
    out = {c: {"matched": 0, "wins": 0, "losses": 0, "championship": 0, "saves": 0}
           for c in ("league", "cup")}
    if not coach:
        return out
    seen = set()
    for match in save.get("schedule") or []:
        if match.get("status") != "done":
            continue
        if coach not in (match.get("blue_team"), match.get("red_team")):
            continue
        cat = cat_of.get(_int_or_none(match.get("series")) or 0, "league")
        bucket = out[cat]
        bucket["matched"] += 1
        if match.get("winner_team") == coach:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        seen.add(cat)
    for i, item in enumerate(series):
        cat = cat_of.get(i, "league")
        if item.get("status") == "done":
            seen.add(cat)
        ranking = item.get("ranking") or []
        champion = _txt((ranking[0] or {}).get("team")) if ranking else ""
        if champion and champion == coach:
            out[cat]["championship"] += 1
    for cat in seen:
        out[cat]["saves"] += 1
    return out


# --------------------------------------------------------------------------- #
# v51：「我的」页要按 春季赛 / 挑战者杯 / 夏季赛 / 年度总决赛 四类分别统计
# --------------------------------------------------------------------------- #
# 赛事 → 四个桶的映射（全部来自赛事模板与站点的真实 key，不做任何猜测）
FOUR_BUCKETS = ("spring", "challenger", "summer", "annual")
FOUR_BUCKET_LABELS = {"spring": "春季赛", "challenger": "挑战者杯",
                      "summer": "夏季赛", "annual": "年度总决赛"}
_SERIES_KEY_TO_BUCKET = {
    "spring": "spring", "league_sab": "spring",
    "challenger": "challenger", "challenger_cup": "challenger",
    "autumn": "summer", "summer": "summer",
    "annual_finals": "annual", "annual": "annual",
}


def _empty_bucket():
    return {"matched": 0, "wins": 0, "losses": 0, "championship": 0, "saves": 0}


def _bucket_of_series(series, index, template):
    """把一站赛事映射到 spring / challenger / summer / annual。"""
    row = series or {}
    for cand in (row.get("key"), row.get("slot"), row.get("stage")):
        key = _txt(cand).strip().lower()
        if key in _SERIES_KEY_TO_BUCKET:
            return _SERIES_KEY_TO_BUCKET[key]
    pipe = _txt(row.get("pipeline")).strip().lower()
    name = _txt(row.get("name"))
    if pipe == "challenger_cup" or "挑战者杯" in name or "挑杯" in name:
        return "challenger"
    if pipe == "annual_finals" or "年度总决赛" in name or "年总" in name:
        return "annual"
    if pipe == "league_sab":
        # 赛程包里两站联赛：第 1 站春、第 2 站夏（按 pipeline 出现顺序）
        if _txt(template) == "annual_package":
            return "spring" if (index or 0) == 0 else "summer"
        return "spring"
    tpl = _txt(template).strip().lower()
    if tpl == "challenger":
        return "challenger"
    if tpl == "annual_finals":
        return "annual"
    if tpl in ("autumn", "summer"):
        return "summer"
    return "spring"


def _four_bucket_stats(save):
    """按 春季赛/挑战者杯/夏季赛/年度总决赛 四类统计一个存档的执教场次/胜负/冠军。"""
    out = {k: _empty_bucket() for k in FOUR_BUCKETS}
    coach = _txt((save.get("coach") or {}).get("team_name"))
    template = _txt(save.get("template"))
    series = save.get("series") or []
    if not series:
        # 单届赛事：整个存档就是一个桶
        bucket = out[_bucket_of_series({}, 0, template)]
        bucket["saves"] += 1
        if coach:
            for match in save.get("schedule") or []:
                if match.get("status") != "done":
                    continue
                if coach not in (match.get("blue_team"), match.get("red_team")):
                    continue
                bucket["matched"] += 1
                if match.get("winner_team") == coach:
                    bucket["wins"] += 1
                else:
                    bucket["losses"] += 1
        champion = _txt(save.get("champion"))
        if coach and champion and champion == coach:
            bucket["championship"] += 1
        return out

    bucket_of = {i: _bucket_of_series(item, i, template) for i, item in enumerate(series)}
    if not coach:
        for i, item in enumerate(series):
            out[bucket_of[i]]["saves"] += 1
        return out
    for match in save.get("schedule") or []:
        if match.get("status") != "done":
            continue
        if coach not in (match.get("blue_team"), match.get("red_team")):
            continue
        idx = _int_or_none(match.get("series")) or 0
        bucket = out[bucket_of.get(idx, "spring")]
        bucket["matched"] += 1
        if match.get("winner_team") == coach:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
    for i, item in enumerate(series):
        bucket = out[bucket_of[i]]
        ranking = item.get("ranking") or []
        champion = _txt((ranking[0] or {}).get("team")) if ranking else ""
        if champion and champion == coach:
            bucket["championship"] += 1
    # 「存档数」= 这个存档覆盖到该类赛事就算 1 次（同一存档不会被重复计数）
    for key in set(bucket_of.values()):
        out[key]["saves"] = 1
    return out


@app.route("/api/tourney/saves", methods=["GET"])
def api_tourney_saves():
    require_tourney()
    mode = _txt(request.args.get("mode")).strip()
    category = _txt(request.args.get("category")).strip()
    raw = _iter_tourneys()
    if mode:
        raw = [s for s in raw if (s.get("mode") or "") == mode]
    if category:
        raw = [s for s in raw if (s.get("category") or "") == category]
    rows = [_tourney_row(s) for s in raw]
    if mode or category:
        app.logger.info("[tourney] 存档列表过滤 mode=%s category=%s → %d 条",
                        mode or "-", category or "-", len(rows))
    groups = {"league": [], "cup": [], "package": []}
    for row in rows:
        groups.setdefault(row.get("category") or "league", []).append(row)
    coach = {"name": "教练", "league_titles": 0, "cup_titles": 0,
             "total_titles": 0, "tier": "无名之辈"}
    split_by_save = {}
    for save in raw:
        if (save.get("category") or "") == "package":
            split_by_save[save.get("save_id")] = _package_category_stats(save)
    for row in rows:
        team = _txt((row.get("coach") or {}).get("team_name"))
        champion = _txt(row.get("champion"))
        if (row.get("category") or "") == "package":
            split = split_by_save.get(row.get("save_id")) or {}
            coach["league_titles"] += _int_or_none((split.get("league") or {}).get("championship")) or 0
            coach["cup_titles"] += _int_or_none((split.get("cup") or {}).get("championship")) or 0
            continue
        if champion and team and champion == team:
            if (row.get("category") or "") == "league":
                coach["league_titles"] += 1
            else:
                coach["cup_titles"] += 1
    coach["total_titles"] = coach["league_titles"] + coach["cup_titles"]
    coach["tier"] = _tourney_tier(coach["total_titles"])
    # v51：四类赛事统计（春季赛 / 挑战者杯 / 夏季赛 / 年度总决赛）
    stats4 = {k: _empty_bucket() for k in FOUR_BUCKETS}
    for save in raw:
        one = _four_bucket_stats(save)
        for key in FOUR_BUCKETS:
            src = one.get(key) or {}
            for field in ("matched", "wins", "losses", "championship", "saves"):
                stats4[key][field] += _int_or_none(src.get(field)) or 0
    for key in FOUR_BUCKETS:
        b = stats4[key]
        b["win_rate"] = round(100.0 * b["wins"] / b["matched"], 1) if b["matched"] else 0.0
        b["label"] = FOUR_BUCKET_LABELS[key]

    stats = {}
    for cat in ("league", "cup"):
        bucket = {"matched": 0, "wins": 0, "losses": 0, "championship": 0, "saves": 0}
        for row in rows:
            row_cat = row.get("category") or ""
            if row_cat == "package":
                split = (split_by_save.get(row.get("save_id")) or {}).get(cat) or {}
                bucket["saves"] += _int_or_none(split.get("saves")) or 0
                bucket["matched"] += _int_or_none(split.get("matched")) or 0
                bucket["wins"] += _int_or_none(split.get("wins")) or 0
                bucket["losses"] += _int_or_none(split.get("losses")) or 0
                bucket["championship"] += _int_or_none(split.get("championship")) or 0
                continue
            if row_cat != cat:
                continue
            bucket["saves"] += 1
            row_stats = row.get("stats") or {}
            bucket["matched"] += _int_or_none(row_stats.get("matched")) or 0
            bucket["wins"] += _int_or_none(row_stats.get("wins")) or 0
            bucket["losses"] += _int_or_none(row_stats.get("losses")) or 0
            bucket["championship"] += _int_or_none(row_stats.get("championship")) or 0
        bucket["win_rate"] = round(100.0 * bucket["wins"] / bucket["matched"], 1) if bucket["matched"] else 0.0
        stats[cat] = bucket
    return ok({"saves": rows, "groups": groups, "coach": coach, "stats": stats,
               "stats4": stats4, "total": len(rows)})


@app.route("/api/tourney/leaderboard", methods=["GET"])
def api_tourney_leaderboard():
    require_tourney()
    rows = _iter_tourneys()
    coaches = {}
    teams = {}
    for save in rows:
        coach = save.get("coach") or {}
        name = _txt(coach.get("name")) or "教练"
        slot = coaches.setdefault(name, {"name": name, "league": 0, "cup": 0, "total": 0})
        team = teams.setdefault(_txt(coach.get("team_name")) or "—",
                                {"name": _txt(coach.get("team_name")) or "—",
                                 "league": 0, "challenger": 0, "annual": 0, "total": 0})
        champion = _txt(save.get("champion"))
        if champion and champion == _txt(coach.get("team_name")):
            category = save.get("category") or ""
            if category == "league":
                slot["league"] += 1
                team["league"] += 1
            elif save.get("template") == "challenger":
                slot["cup"] += 1
                team["challenger"] += 1
            else:
                slot["cup"] += 1
                team["annual"] += 1
    coach_rows = sorted(coaches.values(), key=lambda r: (-r["total"], r["name"]))
    for row in coach_rows:
        row["total"] = row["league"] + row["cup"]
        row["tier"] = _tourney_tier(row["total"])
    team_rows = sorted(teams.values(), key=lambda r: (-r["total"], r["name"]))
    for row in team_rows:
        row["total"] = row["league"] + row["challenger"] + row["annual"]
    return ok({"coaches": coach_rows, "teams": team_rows,
               "note": "榜单只统计本机真实赛程存档，没有全服数据，不臆造数字。"})


@app.route("/api/tourney/new", methods=["POST"])
def api_tourney_new():
    require_tourney()
    payload = _payload_of()
    template_key = _txt(payload.get("template")) or "spring"
    if _kpl_rules_mod.template(template_key) is None:
        fail("未知赛事模板：%s" % template_key, 400)
    # v29：届次（edition）优先——官方赛事名 + 对应赛季数据 + 年总包四站
    edition = None
    edition_id = _txt(payload.get("edition_id")).strip()
    stations = None
    if edition_id:
        edition = _resolve_edition(edition_id)
        if not edition:
            raise APIError("未知赛事版本：%s" % edition_id, 400)
        template_key = edition.get("template") or template_key
        stations = edition.get("stations") or None
        # v29：单赛事模板也用「单站」把官方名称 / 赛季数据带进去
        if not stations and edition.get("name"):
            stations = [{"key": template_key,
                         "label": edition["name"],
                         "league_id": edition.get("league_id")}]
    league_id = _tourney_pick_league(template_key, payload)
    if edition and edition.get("league_id"):
        league_id = edition["league_id"]
    # v31：没走 edition 的老调用方（大模型联赛旧参数 / 好友 PK）也要拿到官方站名
    if template_key == "annual_package" and not stations:
        try:
            stations = data_loader.package_stations(league_id) or None
        except Exception:                                   # noqa: BLE001
            stations = None
    if not league_id:
        fail("没有可用的赛季数据，请先运行 scraper.py", 503)
    teams = _tourney_teams(league_id)
    if template_key == "annual_package":
        # v21：年度赛程包的挑战者杯站要 32 支队，队池在建档时就补齐
        # v29：优先用该届赛程包自己的挑战者杯赛季补位
        cup_league = None
        for st in stations or []:
            if st.get("key") == "challenger":
                cup_league = st.get("league_id")
        teams = _tourney_teams_annual(league_id, cup_league)
    if len(teams) < 4:
        fail("该赛季战队数据不足（%d 支），请重新运行 scraper.py" % len(teams), 503)
    want = _kpl_rules_mod.template_teams(template_key)
    if len(teams) < want:
        app.logger.warning("[tourney] 赛季 %s 只有 %d 支战队，模板需要 %d 支，按实际数量开赛",
                           league_id, len(teams), want)
    mode = _enum_param(payload, "mode", ("career", "friend", "llm"), "career")
    team_mode = _txt(payload.get("team_mode")) or "existing"
    if team_mode not in ("existing", "custom"):
        fail("参数 team_mode 只接受 existing / custom", 400)
    team_name = _txt(payload.get("team_name"))
    coach_name = _txt(payload.get("coach_name")) or "教练"
    if team_mode == "custom":
        if not team_name:
            fail("请输入自定义战队名称", 400)
        if len(team_name) > 5:
            fail("战队名称最多 5 个字", 400)
    else:
        if team_name not in {t["name"] for t in teams}:
            fail("请选择一支参赛战队", 400)
    llm_team = _txt(payload.get("llm_team"))
    if mode == "llm":
        # v47：大模型联赛里玩家不能用官方 KPL 战队（避免胜率影响），大模型战队名固定「DeepSeek」
        others = [t for t in teams if t["name"] != team_name]
        if not others:
            fail("该赛季战队数据不足，无法分配大模型战队", 503)
        llm_team = "DeepSeek"
        # 把其中一支 AI 队伍的显示名改成 DeepSeek（数据仍来自该队的真实赛季数据）
        pick = others[0]
        old_name = pick["name"]
        pick["name"] = llm_team
        for row in teams:
            if row is pick:
                continue
            # 若有队伍重名冲突，直接报错更好排查
            if row["name"] == llm_team:
                fail("战队池里已经存在同名队伍，无法分配大模型战队", 500)
        app.logger.info("[tourney] LLM 联赛分配：玩家=%s 大模型=DeepSeek（原 %s）其余=%d 队交给内置 AI",
                        team_name, old_name, max(0, len(teams) - 2))
    if len(_iter_tourneys()) >= MAX_TOURNEYS:
        fail("存档数量已达上限（%d 个），请先删除旧存档" % MAX_TOURNEYS, 400)
    season_name = ""
    leagues, _default = _leagues_payload()
    for row in leagues:
        if _txt(row.get("league_id")) == league_id:
            season_name = _txt(row.get("name"))
            break
    template_meta = _kpl_rules_mod.template(template_key) or {}
    coach = {"name": coach_name, "team_name": team_name,
             "team_is_custom": team_mode == "custom", "team_logo": None, "team_id": None}
    if team_mode == "existing":
        for row in teams:
            if row["name"] == team_name:
                coach["team_logo"] = row.get("logo_path")
                coach["team_id"] = row.get("team_id")
                break
    save_name = _txt(payload.get("save_name")) or "%s · %s" % (
        season_name or "KPL", template_meta.get("short") or template_key)
    short_label = template_meta.get("short") or ""
    # 赛季名已含模板关键词（如「2025年KPL春季赛」含「春季赛」）时不再叠加，避免重复
    if edition and edition.get("name"):
        event_name = edition["name"]
    elif season_name and short_label and short_label.replace("KPL", "").strip() in season_name:
        event_name = season_name
    elif season_name and short_label:
        event_name = "%s · %s" % (season_name, short_label)
    else:
        event_name = season_name or (template_meta.get("name") or template_key)
    try:
        save = _tournament_mod.new_save(template_key, teams, coach, mode=mode,
                                        save_name=save_name, league_id=league_id,
                                        event_name=event_name, llm_team=llm_team,
                                        stations=stations)
    except Exception as exc:                                  # noqa: BLE001
        app.logger.error("[tourney] 创建赛程失败：%s", exc)
        fail("创建赛程失败：%s" % exc, 400)
    _write_tourney(save)
    app.logger.info("[tourney] 新建存档 %s（模板 %s / 模式 %s / %d 队）",
                    save["save_id"], template_key, mode, len(save.get("participants") or []))
    return ok({"save_id": save["save_id"], "state": _tourney_state(save)}, 201)


@app.route("/api/tourney/state", methods=["GET"])
def api_tourney_state():
    require_tourney()
    save = _load_tourney(_check_tourney_id(request.args.get("save_id")))
    state = _tourney_state(save)
    # v23：内容没变时 _write_tourney 内部会直接跳过写盘（advance 大多数时候是空转）
    _write_tourney(save)
    return ok(state)


@app.route("/api/tourney/draw_commit", methods=["POST"])
def api_tourney_draw_commit():
    """v28：玩家点「开启下一赛程 / 开始淘汰赛抽签」→ 真正开下一站或进淘汰赛。"""
    require_tourney()
    payload = _payload_of()
    save_id = _check_tourney_id(payload.get("save_id"))
    save = _load_tourney(save_id)
    with _TOURNEY_LOCK:
        try:
            draw = _tournament_mod.commit_draw_gate(save)
        except Exception as exc:                              # noqa: BLE001
            app.logger.error("[tourney] 开启赛程失败（%s）：%s", save_id, exc)
            fail("开启赛程失败：%s" % exc, 400)
        _write_tourney(save)
        app.logger.info("[tourney] 闸门开启：save=%s kind=%s label=%s",
                        save_id, (draw or {}).get("kind"), (draw or {}).get("title"))
        state = _tourney_state(save)
    return ok({"draw": draw, "state": state})


@app.route("/api/tourney/breakout", methods=["GET"])
def api_tourney_breakout():
    """v31：年总突围赛选对手状态。"""
    require_tourney()
    save = _load_tourney(_check_tourney_id(request.args.get("save_id")))
    return ok({"pick": _tournament_mod._breakout_pick_view(save)})


@app.route("/api/tourney/breakout_pick", methods=["POST"])
def api_tourney_breakout_pick():
    """v31：选一手对手（不传 opponent 表示让 AI 选）。"""
    require_tourney()
    payload = _payload_of()
    save_id = _check_tourney_id(payload.get("save_id"))
    opponent = payload.get("opponent")
    save = _load_tourney(save_id)
    with _TOURNEY_LOCK:
        try:
            view = _tournament_mod.apply_breakout_pick(save, opponent)
        except Exception as exc:                              # noqa: BLE001
            app.logger.error("[tourney] 突围赛选对手失败（%s）：%s", save_id, exc)
            raise APIError("选对手失败：%s" % exc, 400)
        _write_tourney(save)
        app.logger.info("[tourney] 突围赛选对手：save=%s step=%s pair=%s",
                        save_id, view.get("step"), (view.get("pairs") or [])[-1:])
    return ok({"pick": view})


@app.route("/api/tourney/breakout_commit", methods=["POST"])
def api_tourney_breakout_commit():
    """v31：选完对手 → 开打突围赛。"""
    require_tourney()
    payload = _payload_of()
    save_id = _check_tourney_id(payload.get("save_id"))
    save = _load_tourney(save_id)
    with _TOURNEY_LOCK:
        try:
            result = _tournament_mod.commit_breakout_gate(save)
        except Exception as exc:                              # noqa: BLE001
            app.logger.error("[tourney] 开打突围赛失败（%s）：%s", save_id, exc)
            raise APIError("开打突围赛失败：%s" % exc, 400)
        _write_tourney(save)
        app.logger.info("[tourney] 突围赛开打：save=%s pairs=%s", save_id, result.get("pairs"))
        state = _tourney_state(save)
    return ok({"result": result, "state": state})


@app.route("/career/breakout", methods=["GET"])
def career_breakout_page():
    """v31：年总突围赛「选对手」页面。"""
    return render_template("career_breakout.html", active_tab="career")


@app.route("/api/tourney/schedule", methods=["GET"])
def api_tourney_schedule():
    require_tourney()
    save = _load_tourney(_check_tourney_id(request.args.get("save_id")))
    day = _int_param(request.args, "day", None, minimum=1)
    only_mine = _txt(request.args.get("only_mine")) in ("1", "true", "yes")
    return ok(_tournament_mod.schedule_view(save, day=day, only_mine=only_mine))


@app.route("/api/tourney/standings", methods=["GET"])
def api_tourney_standings():
    require_tourney()
    save = _load_tourney(_check_tourney_id(request.args.get("save_id")))
    return ok(_tournament_mod.standings(save))


@app.route("/api/tourney/bracket", methods=["GET"])
def api_tourney_bracket():
    require_tourney()
    save = _load_tourney(_check_tourney_id(request.args.get("save_id")))
    stage = (request.args.get("stage") or "").strip() or None
    # v13：支持按阶段回看（挑战者杯 32 进 8 的单败图、淘汰赛双败图、年总淘汰赛图）
    app.logger.info("[tourney] 对阵图请求 save=%s stage=%s", save.get("save_id"), stage or "(当前)")
    return ok({"bracket": _tournament_mod.bracket_view(save, stage)})


@app.route("/api/tourney/history", methods=["GET"])
def api_tourney_history():
    """v19 历史回看索引：所有站点（系列赛）× 所有阶段，只读，不写存档。"""
    require_tourney()
    save = _load_tourney(_check_tourney_id(request.args.get("save_id")))
    app.logger.info("[tourney] 历史索引请求 save=%s", save.get("save_id"))
    return ok(_tournament_mod.history_index(save))


@app.route("/api/tourney/history_stage", methods=["GET"])
def api_tourney_history_stage():
    """v19 历史回看明细：某站某阶段的赛程 / 积分榜 / 阶段名次 / 对阵图，只读。"""
    require_tourney()
    save = _load_tourney(_check_tourney_id(request.args.get("save_id")))
    series = _int_param(request.args, "series", 0, minimum=0)
    stage = (request.args.get("stage") or "").strip()
    if not stage:
        return fail("缺少阶段参数 stage")
    try:
        payload = _tournament_mod.history_stage(save, series, stage)
    except _tournament_mod.TourneyError as exc:
        return fail(str(exc))
    app.logger.info("[tourney] 历史阶段请求 save=%s series=%s stage=%s（%d 场）",
                    save.get("save_id"), series, stage, payload.get("total") or 0)
    return ok(payload)


@app.route("/api/tourney/annual", methods=["GET"])
def api_tourney_annual():
    require_tourney()
    save = _load_tourney(_check_tourney_id(request.args.get("save_id")))
    return ok({"annual": _tournament_mod.annual_ranking(save)})


@app.route("/api/tourney/next_match", methods=["GET"])
def api_tourney_next_match():
    require_tourney()
    save = _load_tourney(_check_tourney_id(request.args.get("save_id")))
    return ok({"match": _tournament_mod.next_player_match(save)})


@app.route("/api/tourney/simulate", methods=["POST"])
def api_tourney_simulate():
    require_tourney()
    payload = _payload_of()
    save_id = _check_tourney_id(payload.get("save_id"))
    save = _load_tourney(save_id)
    count = _int_param(payload, "count", 1, minimum=1, maximum=200) or 1
    until_mine = _bool_param(payload, "until_mine", False)
    include_mine = _bool_param(payload, "include_mine", False)
    with _TOURNEY_LOCK:
        try:
            played = _tournament_mod.simulate(save, count=count, until_mine=until_mine,
                                             include_mine=include_mine)
        except Exception as exc:                              # noqa: BLE001
            app.logger.error("[tourney] 模拟失败：%s", exc)
            fail("模拟失败：%s" % exc, 400)
        _write_tourney(save)
        state = _tourney_state(save)
    return ok({"played": played, "state": state})


@app.route("/api/tourney/result", methods=["POST"])
def api_tourney_result():
    require_tourney()
    payload = _payload_of()
    save_id = _check_tourney_id(payload.get("save_id"))
    save = _load_tourney(save_id)
    match_key = _txt(payload.get("match_key"))
    winner_side = _txt(payload.get("winner_side"))
    # v18：前端优先传 winner_team（战队名），后端按存档里的对阵把队名映射成 blue/red，
    # 彻底摆脱「阵营换边导致胜负反了」的问题。
    winner_team = _txt(payload.get("winner_team"))
    score = payload.get("score") if isinstance(payload.get("score"), dict) else None
    bp_match_id = _txt(payload.get("bp_match_id")) or None
    # v9 需求 2②：把内存里的真实 BP 对局整理成记录写进存档（存档可离线复盘）
    bp_record = None
    try:
        # MATCHES 里存的是 {"match": ..., "touched": ...} 包装，不能直接当对局用
        _entry = MATCHES.get(bp_match_id) if bp_match_id else None
        bp_match = (_entry or {}).get("match") if isinstance(_entry, dict) else None
        # v18：胜负与比分一律**以战队名为准**从内存对局反推，忽略前端传来的阵营口径。
        # （阵营每局会因为选边互换，按红蓝记账必然对不上；队名不会变。）
        if isinstance(bp_match, dict):
            try:
                fixture = None
                for one in save.get("schedule") or []:
                    if str(one.get("match_key")) == str(match_key):
                        fixture = one
                        break
                # team_wins 可能还没写进 match（自动 BP/结算路径差异），用引擎函数现算
                try:
                    tw = bp_engine.team_wins(bp_match)          # type: ignore[attr-defined]
                except Exception:                                 # noqa: BLE001
                    tw = bp_match.get("team_wins") or {}
                if fixture and isinstance(tw, dict) and tw:
                    b_name = str(fixture.get("blue_team") or "")
                    r_name = str(fixture.get("red_team") or "")
                    b_wins = int(tw.get(b_name) or 0)
                    r_wins = int(tw.get(r_name) or 0)
                    need = int(fixture.get("bo") or 1) // 2 + 1
                    if b_wins >= need and b_wins > r_wins:
                        winner_side, winner_team = "blue", b_name
                    elif r_wins >= need and r_wins > b_wins:
                        winner_side, winner_team = "red", r_name
                    score = {"blue": b_wins, "red": r_wins}
                    app.logger.info("[tourney] 按队名反推胜负：%s %d : %d %s（need=%d）",
                                    b_name, b_wins, r_wins, r_name, need)
            except Exception as exc:                          # noqa: BLE001
                app.logger.warning("[tourney] 按队名反推胜负失败：%s", exc)
        if isinstance(bp_match, dict):
            hist = bp_match.get("history") or []
            bp_record = {
                "match_id": bp_match_id,
                "bo": bp_match.get("bo"),
                "games": [dict(h) for h in hist],
                "score": bp_match.get("score"),
                "final": bp_match.get("final"),
                "picks": bp_match.get("picks"),
                "bans": bp_match.get("bans"),
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            app.logger.info("[tourney] 附带 BP 记录 match=%s 手数=%d",
                            bp_match_id, len(hist))
    except Exception as exc:                                  # noqa: BLE001
        app.logger.warning("[tourney] 生成 BP 记录失败：%s", exc)
    with _TOURNEY_LOCK:
        try:
            record = _tournament_mod.apply_result(save, match_key, winner_side, score,
                                                  source="bp", bp_match_id=bp_match_id,
                                                  bp_record=bp_record, winner_team=winner_team)
        except Exception as exc:                              # noqa: BLE001
            app.logger.error("[tourney] 提交结果失败：%s", exc)
            fail("提交比赛结果失败：%s" % exc, 400)
        _write_tourney(save)
        state = _tourney_state(save)
    return ok({"record": record, "state": state})


@app.route("/api/tourney/rename", methods=["POST"])
def api_tourney_rename():
    require_tourney()
    payload = _payload_of()
    save = _load_tourney(_check_tourney_id(payload.get("save_id")))
    name = _txt(payload.get("save_name"))
    if not name:
        fail("请输入存档名称", 400)
    save["save_name"] = name[:24]
    _write_tourney(save)
    return ok({"save": _tourney_row(save)})


@app.route("/api/tourney/delete", methods=["POST"])
def api_tourney_delete():
    require_tourney()
    payload = _payload_of()
    save_id = _check_tourney_id(payload.get("save_id"))
    path = _tourney_path(save_id)
    # v48：这里原来用 fail()（只 return 不抛），删不存在的存档也会返回 ok:true —— 删除后用同一 id 再删
    # 还会「成功」，容易让人以为删除接口坏了。改成 raise APIError，真实返回 404/500。
    if not os.path.exists(path):
        raise APIError("存档不存在：%s" % save_id, 404)
    try:
        os.remove(path)
    except OSError as exc:
        raise APIError("删除存档失败：%s" % exc, 500) from exc
    _LAST_WRITTEN.pop(save_id, None)
    app.logger.info("[tourney] 已删除存档 %s", save_id)
    return ok({"deleted": save_id})



@app.route("/api/health", methods=["GET"])
def api_health():
    heroes = dl("load_heroes")
    if isinstance(heroes, dict):
        heroes = heroes.get("heroes")
    heroes_count = len(_as_list(heroes))
    leagues, default_id = _leagues_payload()
    res = league_resources(default_id) if default_id else None

    return ok({
        "version": APP_VERSION,
        "engine_ready": engine_ready(),
        "engine_error": None if engine_ready() else engine_error_message(),
        "data_ready": heroes_count >= 100,
        "heroes": heroes_count,
        "leagues": len(leagues),
        "default_league_id": default_id,
        "default_league_teams": len(res.team_view()) if res else 0,
        "active_matches": active_match_count(),
        "match_ttl_seconds": MATCH_TTL_SECONDS,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
    })



# --------------------------------------------------------------------------- #
# 生涯模式（v3）：存档、赛事枚举、赛程推进
# --------------------------------------------------------------------------- #

CAREER_DIR = os.path.join(DATA_DIR, "career")
CAREER_VERSION = 3
_SAVE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{6,48}$")
_CAREER_LOCK = threading.RLock()

CATEGORY_LABEL = {"league": "联赛", "cup": "杯赛"}
CATEGORY_ORDER = ("league", "cup")
MAX_SAVES = 40


def career_ready():
    return career_engine is not None


def require_career():
    if career_engine is None:
        raise APIError("生涯模式不可用：%s"
                       % _IMPORT_ERRORS.get("game.career_engine", "career_engine 未就绪"), 503)


def _career_dir(create=False):
    if create:
        try:
            os.makedirs(CAREER_DIR, exist_ok=True)
        except OSError as exc:
            raise APIError("生涯存档目录不可写：%s" % exc, 500)
    return CAREER_DIR


def _new_save_id():
    return "cs_" + secrets.token_hex(6)


def _check_save_id(raw):
    text = str(raw or "").strip()
    if not _SAVE_ID_RE.match(text):
        raise APIError("save_id 不合法：%r" % (raw,), 400)
    return text


def _career_path(save_id):
    return os.path.join(_career_dir(), save_id + ".json")


def _load_career(save_id):
    """读存档；不存在 404，内容损坏 500（不静默返回空档）。"""
    save_id = _check_save_id(save_id)
    path = _career_path(save_id)
    if not os.path.isfile(path):
        raise APIError("生涯存档不存在：%s" % save_id, 404)
    try:
        with io.open(path, encoding="utf-8") as fh:
            save = json.load(fh)
    except Exception as exc:
        raise APIError("生涯存档损坏（%s）：%s" % (save_id, exc), 500)
    if not isinstance(save, dict):
        raise APIError("生涯存档损坏（%s）：根节点不是对象" % save_id, 500)
    save["save_id"] = save_id
    return save


def _write_career(save):
    """原子写：先写临时文件再 os.replace，避免中断留下半截 JSON。"""
    require_career()
    save = dict(save or {})
    save_id = _check_save_id(save.get("save_id"))
    save["version"] = CAREER_VERSION
    save["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _career_dir(create=True)
    path = _career_path(save_id)
    tmp = path + ".tmp"
    body = json.dumps(_safe(save), ensure_ascii=False, allow_nan=False, indent=1)
    try:
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise APIError("生涯存档写入失败：%s" % exc, 500)
    return save


def _iter_careers():
    """按更新时间倒序遍历所有存档（损坏的跳过，不阻断列表）。"""
    directory = _career_dir()
    if not os.path.isdir(directory):
        return []
    out = []
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        save_id = name[:-5]
        if not _SAVE_ID_RE.match(save_id):
            continue
        try:
            with io.open(os.path.join(directory, name), encoding="utf-8") as fh:
                save = json.load(fh)
        except Exception:
            continue
        if not isinstance(save, dict):
            continue
        save["save_id"] = save_id
        out.append(save)
    out.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    return out


def _career_event_row(league, team_count):
    name = league.get("name") or ""
    category, fmt = career_engine.classify_league(name)
    meta = career_engine.event_meta(league, team_count)
    meta["category"] = category
    meta["format"] = fmt
    meta["category_label"] = CATEGORY_LABEL.get(category, "赛事")
    meta["format_desc"] = career_engine.format_desc(fmt, team_count)
    return meta


_CAREER_TEAM_ROW_CACHE = {}


def _career_team_rows(league_id):
    """生涯用的轻量战队列表。

    只读 ``team_stats_by_league`` + ``team_meta.json``（都是小文件），
    刻意**不**经过 ``league_resources()`` —— 那会顺带把该赛季 3000 局的
    BP 历史读进内存，8 个赛事串行加载会让「选择赛事」明显卡顿（v4 BUG 修复）。
    读取失败时返回空列表并打日志，绝不让整个接口 500。
    """
    key = str(league_id or "")
    cached = _CAREER_TEAM_ROW_CACHE.get(key)
    if cached is not None:
        return cached

    rows = []
    try:
        stats = dl("load_team_stats", league_id)
    except Exception as exc:                        # noqa: BLE001 - 兜底
        app.logger.warning("[career] 读取 %s team_stats 失败：%s", league_id, exc)
        stats = None
    try:
        meta = dl("load_team_meta") or {}
    except Exception as exc:                        # noqa: BLE001
        app.logger.warning("[career] 读取 team_meta 失败：%s", exc)
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    for team in _as_list((stats or {}).get("teams")):
        if not isinstance(team, dict):
            continue
        team_id = team.get("team_id")
        info = meta.get(str(team_id)) if team_id is not None else None
        info = info if isinstance(info, dict) else {}
        name = team.get("name") or info.get("name")
        if not name:
            continue
        rows.append({
            "team_id": str(team_id) if team_id is not None else None,
            "name": name,
            "abbreviation": team.get("abbreviation") or info.get("abbreviation"),
            "logo_path": info.get("logo_path") or team.get("logo_path"),
            "logo_ok": info.get("logo_ok"),
            "matches": _int_or_none(team.get("matches")),
            "wins": _int_or_none(team.get("wins")),
            "losses": _int_or_none(team.get("losses")),
            "game_diff": _int_or_none(team.get("game_diff")),
            "win_rate": _num_or_none(team.get("win_rate")),
            "blue_win_rate": _num_or_none(team.get("blue_win_rate")),
            "red_win_rate": _num_or_none(team.get("red_win_rate")),
            "rank": _int_or_none(team.get("rank")),
        })
    rows.sort(key=_team_sort_key)
    if rows:
        _CAREER_TEAM_ROW_CACHE[key] = rows
    return rows


def _career_pool(league_id, team_mode, team_name):
    """组装参赛战队池：existing 用该赛季真实战队；custom 顶替排名最后的一支。"""
    rows = _career_team_rows(league_id)
    if not rows:
        raise APIError("该赛事没有战队数据，无法开启生涯", 503)
    if team_mode == "custom":
        custom = {
            "team_id": None,
            "name": team_name,
            "abbreviation": (team_name or "")[:4] or None,
            "logo_path": None,
            "matches": None,
            "wins": None,
            "losses": None,
            "game_diff": None,
            "win_rate": None,
            "blue_win_rate": None,
            "red_win_rate": None,
            "avg_kda": None,
            "rank": None,
        }
        if len(rows) > 4:
            rows = rows[:-1] + [custom]
        else:
            rows = rows + [custom]
        return rows, team_name
    for row in rows:
        if row.get("name") == team_name:
            return rows, team_name
    raise APIError("该赛季没有战队「%s」，请从列表中选择或使用自建战队" % team_name, 400)


def _auto_advance(save):
    """阶段已全部打完就自动进入下一阶段；已是最后一阶段则结束生涯。

    返回 True 表示发生了阶段推进（前端可据此提示）。
    """
    moved = False
    guard = 0
    while career_engine.stage_complete(save) and str(save.get("status")) == "running":
        guard += 1
        if guard > 12:
            break
        before = save.get("stage")
        career_engine.next_stage(save)
        moved = moved or (save.get("stage") != before)
        if save.get("stage") == before:
            break
    return moved


def _career_state_payload(save, league_id=None):
    """生涯状态：一次返回赛季进程页需要的全部数据。"""
    summary = career_engine.summary(save)
    fmt = (save.get("event") or {}).get("format")
    plan_rows = []
    plan = career_engine.stage_plan(fmt)
    current = save.get("stage")
    for idx, (key, label, kind, bo) in enumerate(plan):
        plan_rows.append({
            "key": key,
            "label": label,
            "kind": kind,
            "kind_label": career_engine.STAGE_KIND_LABEL.get(kind, kind),
            "bo": bo,
            "index": idx + 1,
            "done": bool(summary.get("stage_index", 0) > idx
                         or (summary.get("stage") == key and summary.get("stage_complete"))),
            "current": summary.get("stage") == key,
            "rules": career_engine._stage_rules(fmt, key),
        })
    bracket = career_engine.bracket_view(save)
    return {
        "save": summary,
        "plan": plan_rows,
        "schedule": career_engine.schedule_view(save),
        "standings": career_engine.standings(save),
        "bracket": bracket,
        "rules": career_engine._stage_rules(fmt, current) if current else None,
        "draw_order": save.get("draw_order") or [],
        "groups": save.get("groups") or [],
        "options": save.get("options") or {"llm_scope": "off"},
    }


def _career_index_payload():
    """生涯首页：联赛/杯赛统计 + 存档列表 + 段位。"""
    saves = _iter_careers()
    stats = {key: {"matched": 0, "wins": 0, "losses": 0, "championship": 0,
                   "win_rate": None, "saves": 0}
             for key in CATEGORY_ORDER}
    rows = []
    for save in saves:
        event = save.get("event") or {}
        category = event.get("category")
        if category not in stats:
            category = "cup"
        st = save.get("stats") or {}
        bucket = stats[category]
        bucket["saves"] += 1
        for key in ("matched", "wins", "losses", "championship"):
            bucket[key] += _int_or_none(st.get(key)) or 0
        try:
            summ = career_engine.summary(save)
        except Exception:
            summ = {}
        rows.append({
            "save_id": save.get("save_id"),
            "event": event,
            "coach": save.get("coach") or {},
            "status": save.get("status"),
            "stage": summ.get("stage"),
            "stage_label": summ.get("stage_label"),
            "stage_index": summ.get("stage_index"),
            "stage_total": summ.get("stage_total"),
            "stage_complete": summ.get("stage_complete"),
            "progress": summ.get("progress"),
            "current_day": summ.get("current_day"),
            "total_days": summ.get("total_days"),
            "stats": summ.get("stats") or {},
            "champion": summ.get("champion"),
            "next_match": summ.get("next_match"),
            "drawn": bool(save.get("schedule")),
            "updated_at": save.get("updated_at"),
            "created_at": save.get("created_at"),
        })
    for bucket in stats.values():
        bucket["win_rate"] = (round(bucket["wins"] * 100.0 / bucket["matched"], 2)
                              if bucket["matched"] else None)
    total_titles = stats["league"]["championship"] + stats["cup"]["championship"]
    return {
        "saves": rows,
        "stats": stats,
        "coach": {
            "name": (rows[0].get("coach") or {}).get("name") if rows else None,
            "league_titles": stats["league"]["championship"],
            "cup_titles": stats["cup"]["championship"],
            "total_titles": total_titles,
            "tier": career_engine.tier_of(total_titles) if career_engine else None,
        },
        "directories": CATEGORY_LABEL,
    }


def _career_leaderboard():
    """冠军榜：完全基于本机真实存档统计，绝不编造全服数字。"""
    saves = _iter_careers()
    coaches = {}
    teams = {}
    for save in saves:
        event = save.get("event") or {}
        fmt = event.get("format")
        bracket = save.get("bracket") or {}
        champion = bracket.get("champion")
        if not champion:
            continue
        coach = save.get("coach") or {}
        coach_name = coach.get("name") or "教练"
        coach_team = coach.get("team_name")
        category = event.get("category") or "cup"
        # 教练冠军榜只统计「他执教的战队就是本届冠军」的存档；
        # 没夺冠的存档不记冠军（否则等于虚报战绩）。
        if coach_team and coach_team == champion:
            row = coaches.setdefault(coach_name, {"name": coach_name, "league": 0, "cup": 0})
            if category == "league":
                row["league"] += 1
            else:
                row["cup"] += 1
        team = teams.setdefault(champion, {"name": champion, "league": 0,
                                           "challenger": 0, "annual": 0})
        if category == "league":
            team["league"] += 1
        elif fmt == "annual":
            team["annual"] += 1
        else:
            team["challenger"] += 1
    coach_rows = sorted(coaches.values(), key=lambda r: (-(r["league"] + r["cup"]), r["name"]))
    team_rows = sorted(teams.values(),
                       key=lambda r: (-(r["league"] + r["challenger"] + r["annual"]), r["name"]))
    for row in coach_rows:
        row["total"] = row["league"] + row["cup"]
        row["tier"] = career_engine.tier_of(row["total"])
    for row in team_rows:
        row["total"] = row["league"] + row["challenger"] + row["annual"]
    return {
        "coaches": coach_rows,
        "teams": team_rows,
        "note": "榜单仅统计本机真实生涯存档，没有全服数据，不臆造数字。",
    }


# ------------------------------- 页面路由 ---------------------------------- #

@app.route("/career/team", methods=["GET"])
def page_career_team():
    return _render_page("career_team.html")


@app.route("/career/draw", methods=["GET"])
def page_career_draw():
    return _render_page("career_draw.html")


@app.route("/career/season", methods=["GET"])
def page_career_season():
    return _render_page("career_season.html")


# ------------------------------- 赛事枚举 ---------------------------------- #

@app.route("/api/career/events", methods=["GET"])
def api_career_events():
    """赛事列表（v4 加固：单个赛事出错只跳过并记日志，绝不拖死整个页面）。"""
    require_career()
    debug = str(request.args.get("debug") or "") in ("1", "true", "yes")
    timings = []
    leagues = []
    default_id = None
    try:
        leagues, default_id = _leagues_payload()
    except Exception as exc:                                # noqa: BLE001
        app.logger.exception("[career] 读取 leagues.json 失败")
        return fail("赛事索引读取失败：%s（请重新运行 scraper.py）" % exc, 503)
    if not isinstance(leagues, list):
        leagues = []
    app.logger.info("[career] /events 开始：%d 个赛事", len(leagues))

    rows = []
    skipped = []
    for league in leagues:
        started = time.time()
        if not isinstance(league, dict):
            skipped.append({"reason": "赛事条目不是对象", "league": str(league)[:40]})
            continue
        lid = str(league.get("league_id") or "")
        try:
            team_count = len(_career_team_rows(lid))
        except Exception as exc:                            # noqa: BLE001
            app.logger.warning("[career] 赛事 %s 战队读取失败，已跳过：%s", lid, exc)
            skipped.append({"league_id": lid, "reason": "%s: %s" % (type(exc).__name__, exc)})
            rows_timing = {"league_id": lid, "ms": round((time.time() - started) * 1000, 1),
                           "ok": False}
            timings.append(rows_timing)
            continue
        try:
            row = _career_event_row(league, team_count)
        except Exception as exc:                            # noqa: BLE001
            app.logger.warning("[career] 赛事 %s 组装失败，已跳过：%s", lid, exc)
            skipped.append({"league_id": lid, "reason": "%s: %s" % (type(exc).__name__, exc)})
            continue
        rows.append(row)
        timings.append({"league_id": lid, "ms": round((time.time() - started) * 1000, 1),
                        "ok": True, "teams": team_count})
    if not rows:
        return fail("没有读到任何赛事数据，请确认 data/leagues.json 存在且非空"
                    "（可重新运行 python scraper.py）", 503)
    rows.sort(key=lambda r: (CATEGORY_ORDER.index(r["category"])
                             if r["category"] in CATEGORY_ORDER else 9,
                             tuple(-ord(ch) for ch in str(r.get("start") or "")),
                             str(r.get("league_id"))))
    groups = {"league": [], "cup": []}
    for row in rows:
        groups.setdefault(row.get("category") or "cup", []).append(row)
    payload = {"leagues": rows, "groups": groups, "default_league_id": default_id,
               "categories": CATEGORY_LABEL, "skipped": skipped}
    if debug:
        payload["_debug"] = {"timings": timings, "total_ms": round(sum(
            item.get("ms") or 0 for item in timings), 1), "count": len(rows)}
    app.logger.info("[career] /events 完成：%d 个赛事，跳过 %d 个", len(rows), len(skipped))
    return ok(payload)


@app.route("/api/career/teams", methods=["GET"])
def api_career_teams():
    require_career()
    league_id = resolve_league_id(request.args.get("league_id"))
    league = _find_league(league_id) or {}
    rows = _career_team_rows(league_id)
    event = _career_event_row(league, len(rows))
    return ok({"league_id": league_id, "event": event, "total": len(rows), "teams": rows})


# ------------------------------- 存档列表 ---------------------------------- #

@app.route("/api/career/saves", methods=["GET"])
def api_career_saves():
    require_career()
    return ok(_career_index_payload())


@app.route("/api/career/leaderboard", methods=["GET"])
def api_career_leaderboard():
    require_career()
    return ok(_career_leaderboard())


# ------------------------------- 新建生涯 ---------------------------------- #

@app.route("/api/career/new", methods=["POST"])
def api_career_new():
    require_career()
    payload = request_payload()
    league_id = resolve_league_id(payload.get("league_id"))
    league = _find_league(league_id)
    if league is None:
        raise APIError("未知的 league_id：%s" % league_id, 404)

    team_mode = _enum_param(payload, "team_mode", ("existing", "custom"), "existing")
    raw_team = str(payload.get("team_name") or "").strip()
    coach_name = str(payload.get("coach_name") or "").strip() or "教练"
    if len(coach_name) > 12:
        raise APIError("教练昵称最长 12 个字", 400)

    if team_mode == "custom":
        if not raw_team:
            raise APIError("自建战队需要填写战队名称", 400)
        if len(raw_team) > 5:
            raise APIError("战队名称最长 5 个字（当前 %d 字）" % len(raw_team), 400)
    elif not raw_team:
        raise APIError("缺少 team_name", 400)

    pool, coach_team = _career_pool(league_id, team_mode, raw_team)
    if not pool:
        raise APIError("该赛事没有可用战队", 503)

    with _CAREER_LOCK:
        existing = _iter_careers()
        if len(existing) >= MAX_SAVES:
            raise APIError("生涯存档已达上限（%d 个），请先删除旧存档" % MAX_SAVES, 400)
        save = career_engine.create_save(league, pool,
                                         {"name": coach_name, "team_name": coach_team},
                                         seed=None)
        save["save_id"] = _new_save_id()
        save["coach"]["team_is_custom"] = (team_mode == "custom")
        custom_row = next((r for r in pool if r.get("name") == coach_team), None)
        if custom_row:
            save["coach"]["team_logo"] = custom_row.get("logo_path")
            save["coach"]["team_id"] = custom_row.get("team_id")
        # v7 需求 3：外部大模型固定只执教 1 支战队；旧值 all 一律收敛为 single，
        # 不再出现「大模型控制全部队伍」的情况（旧存档读取路径保持兼容）。
        llm_scope = _enum_param(payload, "llm_scope", ("off", "single", "final", "all"), "off")
        if llm_scope == "all":
            app.logger.info("[llm] llm_scope=all 已按 v7 规则收敛为 single（大模型只带 1 支战队）")
            llm_scope = "single"
        save["options"] = {"llm_scope": llm_scope}
        career_engine.draw_groups(save, pool)
        _write_career(save)
        app.logger.info("[career] 新存档 %s：赛事=%s 战队=%s 大模型范围=%s",
                        save["save_id"], league_id, coach_team, llm_scope)

    return ok({"save_id": save["save_id"], "state": _career_state_payload(save)},
              status=201)


@app.route("/api/career/state", methods=["GET"])
def api_career_state():
    require_career()
    with _CAREER_LOCK:
        save = _load_career(request.args.get("save_id"))
        if _auto_advance(save):
            _write_career(save)
        state = _career_state_payload(save)
    return ok(state)


@app.route("/api/career/schedule", methods=["GET"])
def api_career_schedule():
    require_career()
    save = _load_career(request.args.get("save_id"))
    day = _int_param(request.args, "day", None, minimum=1)
    only_mine = _bool_param(request.args, "only_mine", False)
    view = career_engine.schedule_view(save, day=day, only_mine=only_mine)
    return ok({"save_id": save["save_id"], "stage": save.get("stage"), **view})


@app.route("/api/career/standings", methods=["GET"])
def api_career_standings():
    require_career()
    save = _load_career(request.args.get("save_id"))
    return ok({"save_id": save["save_id"],
               "standings": career_engine.standings(save)})


@app.route("/api/career/bracket", methods=["GET"])
def api_career_bracket():
    require_career()
    save = _load_career(request.args.get("save_id"))
    return ok({"save_id": save["save_id"],
               "bracket": career_engine.bracket_view(save)})


@app.route("/api/career/simulate", methods=["POST"])
def api_career_simulate():
    require_career()
    payload = request_payload()
    count = _int_param(payload, "count", 1, minimum=1, maximum=200)
    until_mine = _bool_param(payload, "until_mine", False)
    include_mine = _bool_param(payload, "include_mine", False)
    with _CAREER_LOCK:
        save = _load_career(payload.get("save_id"))
        if save.get("status") == "done":
            raise APIError("该生涯已结束", 409)
        if not save.get("schedule"):
            raise APIError("该存档还没有抽签，无法模拟比赛", 409)
        career_engine.simulate(save, count=count, until_mine=until_mine,
                               include_mine=include_mine)
        played = len(save.get("_last_simulated") or [])
        advanced = _auto_advance(save)
        _write_career(save)
        state = _career_state_payload(save)
    return ok({"played": played, "advanced": advanced, "state": state})


@app.route("/api/career/next_stage", methods=["POST"])
def api_career_next_stage():
    require_career()
    payload = request_payload()
    with _CAREER_LOCK:
        save = _load_career(payload.get("save_id"))
        if save.get("status") == "done":
            raise APIError("该生涯已结束", 409)
        if not career_engine.stage_complete(save):
            raise APIError("当前阶段还没打完，无法进入下一阶段", 409)
        career_engine.next_stage(save)
        advanced = _auto_advance(save)
        _write_career(save)
        state = _career_state_payload(save)
    return ok({"advanced": advanced, "state": state})


@app.route("/api/career/result", methods=["POST"])
def api_career_result():
    """BP 打完一局后回写生涯战绩（arena 结算时调用）。"""
    require_career()
    payload = request_payload()
    winner_side = _enum_param(payload, "winner_side", ("blue", "red"), None)
    winner_team = _txt(payload.get("winner_team"))
    if winner_side is None and not winner_team:
        raise APIError("缺少 winner_side（blue / red）或 winner_team", 400)
    match_key = str(payload.get("match_key") or "").strip()
    if not match_key:
        raise APIError("缺少 match_key", 400)
    score = payload.get("score")
    if isinstance(score, dict):
        score = {"blue": _int_or_none(score.get("blue")) or 0,
                 "red": _int_or_none(score.get("red")) or 0}
    else:
        score = None
    with _CAREER_LOCK:
        save = _load_career(payload.get("save_id"))
        record = career_engine.apply_result(save, match_key, winner_side, score=score,
                                            bp_match_id=payload.get("bp_match_id"),
                                            source="bp", winner_team=winner_team)
        advanced = _auto_advance(save)
        _write_career(save)
        state = _career_state_payload(save)
    return ok({"record": record, "advanced": advanced, "state": state})


@app.route("/api/career/delete", methods=["POST"])
def api_career_delete():
    require_career()
    payload = request_payload()
    save_id = _check_save_id(payload.get("save_id"))
    path = _career_path(save_id)
    with _CAREER_LOCK:
        if not os.path.isfile(path):
            raise APIError("生涯存档不存在：%s" % save_id, 404)
        try:
            os.remove(path)
        except OSError as exc:
            raise APIError("删除存档失败：%s" % exc, 500)
    return ok({"deleted": True, "save_id": save_id})


def _bool_param(payload, key, default=False):
    value = payload.get(key) if payload is not None else None
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    raise APIError("参数 %s 只接受 true / false，收到：%r" % (key, value), 400)




# =========================================================================== #
# v4 · 设置 / 外部大模型 / 朋友 PK
# =========================================================================== #

V4_CONFIG_ERROR = "配置模块未就绪（game/ai_config.py 缺失）"


def _llm_ready() -> bool:
    if ai_config is None:
        return False
    try:
        return bool(ai_config.llm_ready())
    except Exception:                                       # noqa: BLE001
        return False


def _llm_state() -> dict:
    if ai_config is None:
        return {"ready": False, "missing": [V4_CONFIG_ERROR]}
    try:
        return ai_config.llm_state()
    except Exception as exc:                                # noqa: BLE001
        return {"ready": False, "missing": ["%s: %s" % (type(exc).__name__, exc)]}


def _ai_auto_advance(match):
    """AI 代走：内置 AI（默认）或外部大模型，两者互不影响。"""
    if str(match.get("ai_engine") or "builtin") != "llm" or llm_coach is None:
        return bp_engine.auto_advance(match)

    ctx = None
    try:
        ctx = data_loader.league_context(match.get("league_id"))
    except Exception:                                       # noqa: BLE001
        ctx = None

    guard = 0
    while match.get("phase") == "bp" and not match.get("finished") and guard < 40:
        guard += 1
        side = match.get("side")
        role = "ai"
        if match.get("mode") == "ai":
            role = "ai" if side != match.get("player_side") else "player"
        if role != "ai":
            break
        if str(side) != str(match.get("side")):
            break
        moves = _moves_for(match)
        if moves is None:
            break
        try:
            decision = llm_coach.decide(match, side, ctx,
                                        action=match.get("action"),
                                        logger=app.logger)
        except Exception as exc:                            # noqa: BLE001
            raise APIError(str(exc), 400) from exc
        _record_ai_move(match, decision)
        result = bp_engine.apply_action(match, int(decision["ename"]), match.get("action"))
        if isinstance(result, dict) and result is not match:
            match.clear()
            match.update(result)
    return match


def _moves_for(match):
    """当前该谁动：返回 None 表示不该继续（玩家回合 / 已结束）。"""
    if match.get("phase") != "bp" or match.get("finished") or match.get("series_over"):
        return None
    side = match.get("side")
    if match.get("mode") != "ai":
        return side
    return side if side != match.get("player_side") else None


def _record_ai_move(match, decision):
    row = {
        "step": match.get("step"),
        "side": match.get("side"),
        "action": match.get("action"),
        "ename": decision.get("ename"),
        "cname": decision.get("cname"),
        "source": decision.get("source"),
        "note": decision.get("note"),
        "latency_ms": decision.get("latency_ms"),
    }
    moves = match.get("ai_moves")
    if not isinstance(moves, list):
        moves = []
        match["ai_moves"] = moves
    moves.append(row)
    if len(moves) > 40:
        del moves[: len(moves) - 40]



# ------------------------------- 设置 ------------------------------------- #

@app.route("/api/settings/scoring", methods=["GET"])
def api_settings_scoring_get():
    if scoring_rules is None:
        raise APIError("评分细则模块未就绪（game/scoring_rules.py 缺失）", 503)
    try:
        return ok(scoring_rules.state())
    except Exception as exc:                            # noqa: BLE001
        raise APIError("读取评分细则失败：%s" % exc, 500) from exc


@app.route("/api/settings/scoring", methods=["POST", "PUT"])
def api_settings_scoring_save():
    if scoring_rules is None:
        raise APIError("评分细则模块未就绪（game/scoring_rules.py 缺失）", 503)
    payload = request_payload()
    patch = payload.get("rules") if isinstance(payload.get("rules"), dict) else payload
    try:
        rules = scoring_rules.save_rules(patch)
    except Exception as exc:                            # noqa: BLE001
        raise APIError("保存评分细则失败：%s" % exc, 500) from exc
    app.logger.info("[scoring] 评分细则已更新：%s", rules.get("weights"))
    out = scoring_rules.state()
    out["rules"] = rules
    return ok(out)


@app.route("/api/settings/scoring/reset", methods=["POST"])
def api_settings_scoring_reset():
    if scoring_rules is None:
        raise APIError("评分细则模块未就绪（game/scoring_rules.py 缺失）", 503)
    try:
        rules = scoring_rules.reset_rules()
    except Exception as exc:                            # noqa: BLE001
        raise APIError("恢复默认失败：%s" % exc, 500) from exc
    app.logger.info("[scoring] 评分细则已恢复默认")
    out = scoring_rules.state()
    out["rules"] = rules
    return ok(out)


@app.route("/settings", methods=["GET"])
def page_settings():
    return _render_page("settings.html")


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    if ai_config is None:
        raise APIError(V4_CONFIG_ERROR, 503)
    try:
        payload = ai_config.public_config()
    except Exception as exc:                                # noqa: BLE001
        raise APIError("读取配置失败：%s" % exc, 500) from exc
    payload["llm_state"] = _llm_state()
    if scoring_rules is not None:
        try:
            payload["scoring"] = scoring_rules.state()
        except Exception:                               # noqa: BLE001
            app.logger.info("[scoring] 读取评分细则失败（不影响设置页）", exc_info=True)
    return ok(payload)


@app.route("/api/settings", methods=["POST", "PUT"])
def api_settings_save():
    if ai_config is None:
        raise APIError(V4_CONFIG_ERROR, 503)
    payload = request_payload()
    try:
        result = ai_config.save_config(payload)
    except Exception as exc:                                # noqa: BLE001
        raise APIError("保存配置失败：%s" % exc, 400) from exc
    result["llm_state"] = _llm_state()
    return ok(result)


@app.route("/api/settings/llm_test", methods=["POST"])
def api_settings_llm_test():
    if llm_coach is None:
        raise APIError("大模型模块未就绪（game/llm_coach.py 缺失）", 503)
    try:
        result = llm_coach.probe()
    except Exception as exc:                                # noqa: BLE001
        result = {"ok": False, "kind": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
    return ok(result)




# ------------------- v9 补丁：服务器监听地址（IP / 端口） ------------------- #
# 用户会把项目部署到云服务器，用手机从外部访问：这里提供读写接口，
# 配置存在 data/server_config.json，改完需要重启进程才生效（启动时读取）。

SERVER_RESTART_NOTE = "已保存。请关闭当前窗口并重新运行 start.bat / start.sh，新地址才会生效。"


def _require_server_config():
    if server_config is None:
        raise APIError("服务器配置模块未就绪（game/server_config.py 缺失）", 503)
    return server_config


# 进程实际监听的地址（__main__ 启动时写入），用来判断「改了但还没重启」
SERVER_RUNTIME = {"host": None, "port": None}


def _runtime_bind():
    """返回 (实际监听 host, port, 是否已知)。未通过 __main__ 启动（如被 WSGI 托管）时返回 None。"""
    host = SERVER_RUNTIME.get("host")
    port = SERVER_RUNTIME.get("port")
    if host is None or port is None:
        return None, None, False
    return host, port, True


@app.route("/api/server/config", methods=["GET"])
def api_server_config():
    module = _require_server_config()
    cfg = module.load()
    run_host, run_port, known = _runtime_bind()
    shown_host, shown_port = (run_host, run_port) if known else (cfg["host"], cfg["port"])
    return ok({
        "config": cfg,
        "effective": {"host": shown_host, "port": shown_port},
        "restart_required": known and (cfg["host"] != run_host or cfg["port"] != run_port),
        "addresses": module.access_urls({"host": shown_host, "port": shown_port}),
        "note": "0.0.0.0 表示允许外部访问；改成 127.0.0.1 就只有本机能打开。改完需重启进程。",
    })


@app.route("/api/server/config", methods=["POST", "PUT"])
def api_server_config_save():
    module = _require_server_config()
    payload = request_payload()
    host = module.normalize_host(payload.get("host"), "")
    port = module.normalize_port(payload.get("port"), 0)
    if not host:
        raise APIError("监听地址不合法（示例：0.0.0.0 / 127.0.0.1 / 192.168.1.10）", 400)
    if not port:
        raise APIError("端口不合法（必须是 1~65535 的整数）", 400)
    ok_saved, message = module.save({"host": host, "port": port})
    if not ok_saved:
        raise APIError(message, 500)
    app.logger.info("[server] 监听配置已更新为 %s:%d（重启后生效）", host, port)
    run_host, run_port, known = _runtime_bind()
    shown_host, shown_port = (run_host, run_port) if known else (host, port)
    return ok({
        "ok": True,
        "config": module.load(),
        "effective": {"host": shown_host, "port": shown_port},
        "restart_required": known and (host != run_host or port != run_port),
        "addresses": module.access_urls({"host": host, "port": port}),
        "note": SERVER_RESTART_NOTE,
    })


@app.route("/api/server/addresses", methods=["GET"])
def api_server_addresses():
    module = _require_server_config()
    eff = module.effective()
    return ok({"effective": {"host": eff["host"], "port": eff["port"]},
               "addresses": module.access_urls(eff)})


# ------------------------------- 朋友 PK ---------------------------------- #

_ROOM_HANDLE = {"handle": None, "room": None, "port": None, "error": ""}
_ROOM_LOCK = threading.RLock()


@app.route("/friend", methods=["GET"])
def page_friend():
    return _render_page("friend.html")


@app.route("/api/room/local_ips", methods=["GET"])
def api_room_local_ips():
    ips = []
    if room_server is not None:
        try:
            ips = room_server.local_ips()
        except Exception:                                   # noqa: BLE001
            ips = []
    return ok({"ips": ips or ["127.0.0.1"]})


@app.route("/api/room/host/start", methods=["POST"])
def api_room_host_start():
    if room_engine is None or room_server is None:
        raise APIError("联机模块未就绪（game/room_engine.py / room_server.py 缺失）", 503)
    payload = request_payload()
    port = _int_param(payload, "port", 5001, minimum=1024, maximum=65535)
    mode = _enum_param(payload, "mode", ("single", "league"), "single")
    league_id = resolve_league_id(payload.get("league_id"))
    league = _find_league(league_id) or {}
    name = str(payload.get("name") or "房主").strip()[:12] or "房主"
    team = payload.get("team")

    with _ROOM_LOCK:
        if _ROOM_HANDLE["handle"] is not None:
            raise APIError("已经有一个房间在运行（端口 %s），请先关闭" % _ROOM_HANDLE["port"], 409)
        try:
            room = room_engine.Room(mode=mode, league_id=league_id,
                                    league_name=league.get("name") or league_id,
                                    host_name=name, host_team=team, port=port)
            room_engine.register(room)
            handle = room_server.start_server(room, port)
        except room_server.RoomServerError as exc:
            room_engine.drop_room(getattr(locals().get("room"), "room_id", ""))
            raise APIError(str(exc), 400) from exc
        except Exception as exc:                            # noqa: BLE001
            raise APIError("创建房间失败：%s: %s" % (type(exc).__name__, exc), 500) from exc
        _ROOM_HANDLE.update({"handle": handle, "room": room, "port": port, "error": ""})
        ips = []
        try:
            ips = room_server.local_ips()
        except Exception:                                   # noqa: BLE001
            pass
        app.logger.info("[room] 房间 %s 已在 0.0.0.0:%s 监听（mode=%s）",
                        room.room_id, port, mode)
        try:
            _start_room_guard()
        except Exception:                                  # noqa: BLE001
            app.logger.exception("[room] 房间守护线程启动失败（不影响使用）")
        state = room.state(room.host_pid)
        return ok({"room_id": room.room_id, "port": port, "mode": mode,
                   "league_id": league_id, "league_name": league.get("name") or league_id,
                   "ips": ips or ["127.0.0.1"], "pid": room.host_pid,
                   "access": ["http://%s:%s" % (ip, port) for ip in (ips or ["127.0.0.1"])],
                   "state": state})


_ROOM_GUARD = {"thread": None, "stop": None, "restarts": 0, "last_error": ""}


def _room_guard_loop(stop_event, interval=3.0):
    """v52：房间端口守护。

    用户反馈「建好房间后端口会自己关掉」。除了让房间线程内部永不退出（room_server），
    这里再兜一层：每 3 秒探一次端口，发现线程死了或端口不再监听，就原地重新监听同一个端口，
    房间对象（玩家、赛程、比分）全部保留，不会丢进度。
    """
    while not stop_event.is_set():
        try:
            stop_event.wait(interval)
            if stop_event.is_set():
                break
            with _ROOM_LOCK:
                handle = _ROOM_HANDLE.get("handle")
                room = _ROOM_HANDLE.get("room")
                port = _ROOM_HANDLE.get("port")
            if handle is None or room is None or not port:
                continue
            if handle.alive() and _port_in_use(int(port)):
                continue
            app.logger.warning("[room] 检测到房间端口 %s 异常（线程存活=%s），正在自动恢复",
                               port, handle.alive())
            try:
                handle.stop()
            except Exception:                              # noqa: BLE001
                pass
            try:
                new_handle = room_server.start_server(room, int(port))
            except Exception as exc:                       # noqa: BLE001
                _ROOM_GUARD["last_error"] = str(exc)
                app.logger.warning("[room] 端口 %s 自动恢复失败：%s", port, exc)
                with _ROOM_LOCK:
                    _ROOM_HANDLE.update({"handle": None, "error": str(exc)})
                continue
            with _ROOM_LOCK:
                _ROOM_HANDLE.update({"handle": new_handle, "error": ""})
            _ROOM_GUARD["restarts"] += 1
            _ROOM_GUARD["last_error"] = ""
            app.logger.info("[room] 端口 %s 已自动恢复（第 %d 次）", port, _ROOM_GUARD["restarts"])
        except Exception:                                  # noqa: BLE001
            app.logger.exception("[room] 房间守护线程异常")


def _start_room_guard():
    threading = __import__("threading")
    with _ROOM_LOCK:
        if _ROOM_GUARD.get("thread") is not None:
            return
        stop_event = threading.Event()
        th = threading.Thread(target=_room_guard_loop, args=(stop_event,),
                              name="kpl-room-guard", daemon=True)
        th.start()
        _ROOM_GUARD["thread"] = th
        _ROOM_GUARD["stop"] = stop_event


def _stop_room_guard():
    with _ROOM_LOCK:
        ev = _ROOM_GUARD.get("stop")
        _ROOM_GUARD["thread"] = None
        _ROOM_GUARD["stop"] = None
    if ev is not None:
        try:
            ev.set()
        except Exception:                                  # noqa: BLE001
            pass


@app.route("/api/room/host/stop", methods=["POST"])
def api_room_host_stop():
    with _ROOM_LOCK:
        handle = _ROOM_HANDLE.get("handle")
        room = _ROOM_HANDLE.get("room")
        if handle is None:
            return ok({"stopped": False, "reason": "当前没有运行中的房间"})
        try:
            handle.stop()
        except Exception as exc:                            # noqa: BLE001
            app.logger.warning("[room] 关闭房间服务失败：%s", exc)
        if room is not None and room_engine is not None:
            room_engine.drop_room(room.room_id)
        _ROOM_HANDLE.update({"handle": None, "room": None, "port": None, "error": ""})
        _stop_room_guard()
        return ok({"stopped": True})


@app.route("/api/room/host/status", methods=["GET"])
def api_room_host_status():
    with _ROOM_LOCK:
        room = _ROOM_HANDLE.get("room")
        if room is None:
            return ok({"running": False})
        pid = str(request.args.get("pid") or room.host_pid)
        try:
            state = room.state(pid)
        except Exception:                                   # noqa: BLE001
            state = None
        return ok({"running": True, "room_id": room.room_id, "port": _ROOM_HANDLE["port"],
                   "mode": room.mode, "state": state})


def _port_in_use(port: int) -> bool:
    """端口是否仍在监听（Windows 下 SO_REUSEADDR 会让 bind 假成功，所以用 connect 探测）。"""
    import socket as _socket
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        sock.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except Exception:                                   # noqa: BLE001
            pass


def _room_port_view() -> Dict[str, Any]:
    """v7 需求 2：联机端口占用面板的数据源。"""
    with _ROOM_LOCK:
        room = _ROOM_HANDLE.get("room")
        port = _ROOM_HANDLE.get("port")
    if room is None or not port:
        return {"ports": [], "count": 0}
    players = []
    try:
        for row in (room.players or {}).values():
            players.append({"name": row.get("name") or "玩家",
                            "team": row.get("team") or "",
                            "online": bool(row.get("online")),
                            "role": "房主" if row.get("pid") == room.host_pid else "玩家"})
    except Exception:                                       # noqa: BLE001
        players = []
    league = getattr(room, "league", None) or {}
    online = sum(1 for p in players if p.get("online"))
    return {
        "ports": [{
            "port": int(port),
            "room_id": room.room_id,
            "mode": room.mode,
            "template": getattr(room, "template", None),
            "league_name": league.get("stage_label") or "",
            "players": len(players),
            "online": online,
            "max_players": getattr(room, "max_players", 2),
            "started": bool(room.match is not None or league),
            "closable": True,
            "access": ["http://%s:%s" % (ip, port) for ip in _room_ips()],
        }],
        "count": 1,
    }


def _room_ips() -> List[str]:
    if room_server is None:
        return ["127.0.0.1"]
    try:
        return room_server.local_ips() or ["127.0.0.1"]
    except Exception:                                       # noqa: BLE001
        return ["127.0.0.1"]


@app.route("/api/room/ports", methods=["GET"])
def api_room_ports():
    """列出当前被本程序占用的联机端口 / 房间信息（v7 需求 2）。"""
    data = _room_port_view()
    probe = []
    for row in data["ports"]:
        probe.append({"port": row["port"], "in_use": _port_in_use(row["port"])})
    data["probe"] = probe
    data["guard"] = {"restarts": _ROOM_GUARD.get("restarts", 0),
                     "last_error": _ROOM_GUARD.get("last_error", "")}
    data["note"] = "只统计本程序自己拉起的联机房间；端口被其它程序占用时这里看不到，可用 netstat 检查。"
    return ok(data)


@app.route("/api/room/ports/close", methods=["POST"])
def api_room_ports_close():
    """按端口关闭联机服务并释放端口（v7 需求 2）。"""
    payload = request_payload()
    want = payload.get("port")
    try:
        want = int(want)
    except (TypeError, ValueError):
        raise APIError("参数 port 必须是整数端口号", 400)
    with _ROOM_LOCK:
        handle = _ROOM_HANDLE.get("handle")
        room = _ROOM_HANDLE.get("room")
        cur = _ROOM_HANDLE.get("port")
        if handle is None or cur is None:
            app.logger.info("[room] 关闭端口 %s：当前没有运行中的房间", want)
            return ok({"closed": False, "port": want, "reason": "当前没有运行中的房间"})
        if int(cur) != want:
            raise APIError("端口 %s 不是本程序占用的联机端口（当前是 %s）" % (want, cur), 404)
        closed = False
        try:
            handle.stop()
            closed = True
        except Exception as exc:                             # noqa: BLE001
            app.logger.warning("[room] 关闭端口 %s 失败：%s", want, exc)
        if room is not None and room_engine is not None:
            room_engine.drop_room(room.room_id)
        _ROOM_HANDLE.update({"handle": None, "room": None, "port": None, "error": ""})
        _stop_room_guard()
        app.logger.info("[room] 端口 %s 已释放（closed=%s）", want, closed)
        return ok({"closed": closed, "port": want,
                   "in_use": _port_in_use(want)})


# ------------------------------- 本地赛事资源（v5 需求 8） ------------------------------- #

def require_resources():
    if resource_manager is None:
        raise APIError("资源管理模块未就绪（game/resource_manager.py 缺失）", 503)
    return resource_manager


@app.route("/api/resources", methods=["GET"])
def api_resources():
    """本地资源现状（**不联网**，随时可调）。"""
    rm = require_resources()
    try:
        return ok(rm.status())
    except Exception as exc:                                # noqa: BLE001
        app.logger.warning("[resources] 读取资源状态失败：%s", exc)
        return fail("读取本地资源状态失败：%s" % exc, 500)


@app.route("/api/resources/check", methods=["GET", "POST"])
def api_resources_check():
    """校验本地文件完整性（大小/哈希）。不联网。"""
    rm = require_resources()
    log = []
    try:
        data = rm.check(log)
    except Exception as exc:                                # noqa: BLE001
        app.logger.warning("[resources] 校验失败：%s", exc)
        return fail("校验本地资源失败：%s（本地资源仍可正常使用）" % exc, 500)
    app.logger.info("[resources] 校验完成：%s", data.get("checked"))
    data["ok"] = True
    data["log"] = log
    return ok(data)


@app.route("/api/resources/update", methods=["GET", "POST"])
def api_resources_update():
    """增量更新：只抓「已结束 + 本地缺失/损坏」的比赛资源。"""
    rm = require_resources()
    body = request.get_json(silent=True) or {}
    limit = _int_or_none(body.get("limit"))
    if limit is None:
        limit = resource_manager.DEFAULT_LIMIT
    limit = max(1, min(50, int(limit)))
    leagues = body.get("league_ids")
    if not isinstance(leagues, list):
        leagues = None
    try:
        data = rm.update(limit=limit, league_ids=leagues)
    except Exception as exc:                                # noqa: BLE001
        app.logger.warning("[resources] 增量更新失败：%s", exc)
        return fail("增量更新失败：%s（本地资源仍可正常使用）" % exc, 500)
    app.logger.info("[resources] 增量更新：%s", data.get("update"))
    data["ok"] = True
    return ok(data)


# ------------------------------- 大模型 AI -------------------------------- #

@app.route("/llm", methods=["GET"])
def page_llm():
    return _render_page("llm.html")


@app.route("/api/llm/status", methods=["GET"])
def api_llm_status():
    state = _llm_state()
    cfg = {}
    if ai_config is not None:
        try:
            cfg = ai_config.load_config().get("llm") or {}
        except Exception:                                   # noqa: BLE001
            cfg = {}
    return ok({"llm": state, "model": cfg.get("model"),
               "endpoint": cfg.get("endpoint"),
               "league_scope": cfg.get("league_scope") or "single_team"})


# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 统一错误处理
# --------------------------------------------------------------------------- #

@app.errorhandler(APIError)
def _handle_api_error(exc):
    # 注意：fail() 现在会抛 APIError，处理器里必须直接构造响应
    return json_response({"ok": False, "error": str(exc.message)}, exc.status)


if career_engine is not None and hasattr(career_engine, "CareerError"):
    @app.errorhandler(career_engine.CareerError)
    def _handle_career_error(exc):
        """生涯引擎的业务异常一律 400：请求本身有问题，不是服务器内部错误。"""
        return json_response({"ok": False, "error": "生涯操作失败：%s" % exc}, 400)


@app.errorhandler(HTTPException)
def _handle_http_exception(exc):
    code = exc.code or 500
    message = exc.description or exc.name
    if code == 404 and (request.path or "").startswith("/api/"):
        message = "接口不存在：%s" % request.path
    # 处理器里不能用 fail()（它现在会抛 APIError）
    return json_response({"ok": False, "error": str(message)}, code)


@app.errorhandler(Exception)
def _handle_unexpected(exc):
    traceback.print_exc()
    if not engine_ready():
        msg = ("服务器内部错误：%s: %s（另外：%s）"
               % (exc.__class__.__name__, exc, engine_error_message()))
    else:
        msg = "服务器内部错误：%s: %s" % (exc.__class__.__name__, exc)
    return json_response({"ok": False, "error": msg}, 500)


# --------------------------------------------------------------------------- #
# 启动自检
# --------------------------------------------------------------------------- #

def _say(message):
    try:
        print(message)
    except UnicodeEncodeError:  # Windows GBK 控制台兜底
        try:
            sys.stdout.write(message.encode("utf-8", "replace").decode("ascii", "replace") + "\n")
        except Exception:
            pass


def _banner(title, lines):
    _say("=" * 70)
    _say("  " + title)
    for line in lines:
        _say("     " + line)
    _say("=" * 70)


def startup_check():
    """数据未就绪时醒目提示，但仍然启动（前端显示空态）。"""
    heroes = dl("load_heroes")
    if isinstance(heroes, dict):
        heroes = heroes.get("heroes")
    heroes_count = len(_as_list(heroes))
    leagues, default_id = _leagues_payload()

    if heroes_count < 100:
        _banner("⚠  数据未就绪，请先运行 python scraper.py",
                ["data/heroes.json 英雄数 = %d（契约要求 >= 100）" % heroes_count,
                 "前端会显示空态，API 返回空列表而不是 500。"])
    else:
        _say("[数据] heroes=%d  leagues=%d  default_league_id=%s"
             % (heroes_count, len(leagues), default_id))
        if default_id:
            res = league_resources(default_id)
            _say("[数据] %s: teams=%d players=%d bp_matches=%d real_bp_steps=%s"
                 % (default_id, len(res.team_view()), len(res.player_view()),
                    len(res.bp_matches), res.real_bp_step_count()))

    if not engine_ready():
        _banner("⚠  游戏引擎未就绪", [
            engine_error_message(),
            "BP 相关接口（/api/bp/*）返回 503，等待 game/ 包交付后再联调。"])

    missing = [name for name in ("index.html", "arena.html", "data_center.html", "career.html")
               if not os.path.exists(os.path.join(TEMPLATE_DIR, name))]
    if missing:
        _say("[模板] templates/ 缺少：%s → 对应页面显示后端占位页"
             % "、".join(missing))


# --------------------------------------------------------------------------- #
# v10：双击 exe 启动健壮性（端口占用 / 已有一个在跑 / 任何异常都不闪退）
# --------------------------------------------------------------------------- #


def _port_free(port: int) -> bool:
    """本机 127.0.0.1:port 是否空闲（能连上说明被占用）。"""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.35)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) != 0
    except Exception:  # noqa: BLE001
        return True
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


def _is_our_game(port: int) -> bool:
    """该端口上跑的是不是本游戏（查 /api/health）。"""
    import json as _json
    import urllib.request

    try:
        url = "http://127.0.0.1:%d/api/health" % int(port)
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        return bool(data.get("ok")) and "heroes" in data
    except Exception:  # noqa: BLE001
        return False


def _pause_before_exit(message: str) -> None:
    """出问题时把窗口留住，别一闪而过。"""
    _say("")
    _say("=" * 62)
    for _line in str(message).splitlines():
        _say(_line)
    _say("=" * 62)
    try:
        if sys.stdin and sys.stdin.isatty():
            input("按回车键关闭窗口 ...")
        else:
            import time as _time

            _time.sleep(8)
    except Exception:  # noqa: BLE001  EOF / 无控制台时直接退出
        pass

# ---------------------------------------------------------------------------
# v32-4：官方数据外置 + 数据包导入
#
# 打包后的 exe 不再"把数据写死在里面"：真实数据始终从 exe 同级目录的 data/ 读取
# （见 game/paths.py）。官方出了 2027 赛季时，只需要：
#   1) 在一台装了 Python 的电脑上跑 `python scraper.py`（会抓最新赛季）
#   2) 把 data/ 里那几份 JSON 压成 zip（本页也有说明）
#   3) 在游戏里「数据中心 → 更新数据」上传这个 zip
# 整个过程不需要重新编译 exe。
# ---------------------------------------------------------------------------

_DATA_IMPORT_ITEMS = (
    "heroes.json",
    "hero_relations.json",
    "leagues.json",
    "team_meta.json",
    "kpl_rules.json",
    "hero_stats_by_league",
    "player_stats_by_league",
    "team_stats_by_league",
    "bp_history_by_league",
    "resources",
)


def _data_root() -> str:
    try:
        return DATA_DIR
    except Exception:                                        # noqa: BLE001
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


# 每个目录一份缓存（以前只存一份，4 个目录互相覆盖 → 每次都重新解析，最慢的 bp_history 要 230ms+）
_COUNT_CACHE: Dict[str, Any] = {}
_COUNT_CACHE_LOCK = threading.Lock()


def _dir_signature(path: str):
    """目录指纹：文件名 + mtime + size。用来判断缓存是否还能用。"""
    try:
        names = sorted(os.listdir(path))
    except Exception:                                        # noqa: BLE001
        return ()
    sig = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            st = os.stat(os.path.join(path, name))
            sig.append((name, int(st.st_mtime), st.st_size))
        except OSError:
            continue
    return tuple(sig)


def _count_json_rows(root: str, rel: str) -> Dict[str, int]:
    """统计某个按赛季分表的目录：{league_id: 条目数}。

    v45：带 mtime+size 指纹缓存。数据没变就直接返回上次结果，
    避免「更新数据」页每次打开都重新解析 30 多个 JSON（原来 200~240ms）。
    """
    path = os.path.join(root, rel)
    if not os.path.isdir(path):
        return {}
    sig = _dir_signature(path)
    key = os.path.abspath(path)
    with _COUNT_CACHE_LOCK:
        hit = _COUNT_CACHE.get(key)
        if hit and hit[0] == sig:
            return dict(hit[1])
    out: Dict[str, int] = {}
    for name, _mtime, _size in sig:
        try:
            payload = json.loads(io.open(os.path.join(path, name), encoding="utf-8").read())
        except Exception:                                    # noqa: BLE001
            continue
        rows = payload.get("heroes") or payload.get("players") or payload.get("teams") or payload.get("matches") or []
        out[name[:-5]] = len(rows)
    with _COUNT_CACHE_LOCK:
        _COUNT_CACHE[key] = (sig, dict(out))
    return out


# --------------------------------------------------------------------------- #
# v33：应用内一键「更新并抓取官方数据」
# 玩家在「更新数据」页点一下，本进程后台线程去跑 scraper，不再要求玩家自己开终端。
# 防拉黑：同一时刻只允许一个任务，且两次抓取之间强制冷静期；默认复用抓取缓存
# （force=False），只补新增赛事/英雄，请求量最小。
# --------------------------------------------------------------------------- #
_REFRESH_LOCK = threading.Lock()
_REFRESH_STATE = {
    "running": False,
    "started_at": 0.0,
    "finished_at": 0.0,
    "ok": None,
    "error": "",
    "phase": "",
    "log": [],
}
REFRESH_MIN_INTERVAL = 30 * 60          # 两次抓取之间的最小间隔（秒）
REFRESH_LOG_LIMIT = 200


def _refresh_snapshot():
    with _REFRESH_LOCK:
        raw = dict(_REFRESH_STATE)
        raw["log"] = list(raw.get("log") or [])
    now = time.time()
    remain = 0
    if raw.get("finished_at"):
        remain = max(0, int(REFRESH_MIN_INTERVAL - (now - raw["finished_at"])))
    raw["cooldown"] = remain
    raw["min_interval"] = REFRESH_MIN_INTERVAL
    raw["next_allowed_in"] = remain
    return raw


def _refresh_push(msg):
    line = str(msg or "").rstrip()
    if not line:
        return
    with _REFRESH_LOCK:
        logs = _REFRESH_STATE.setdefault("log", [])
        logs.append(line)
        if len(logs) > REFRESH_LOG_LIMIT:
            del logs[: len(logs) - REFRESH_LOG_LIMIT]
        _REFRESH_STATE["phase"] = line[:80]


def _refresh_worker(force: bool):
    """后台线程：跑 scraper，跑完清缓存重载规则。"""
    ok_flag = False
    err = ""
    try:
        import importlib

        scraper = importlib.import_module("scraper")
        # 打包后 scraper 的目录常量指向资源目录（只读），统一改到可写目录
        try:
            from pathlib import Path as _Path

            scraper.DATA_DIR = _Path(DATA_DIR)
            scraper.STATIC_IMG_HEROES = _Path(IMAGES_DIR) / "heroes"
            scraper.STATIC_IMG_TEAMS = _Path(IMAGES_DIR) / "teams"
            scraper.CACHE_DIR = _Path(DATA_DIR) / ".scrape_cache"
        except Exception:  # noqa: BLE001
            app.logger.warning("[refresh] 重定向 scraper 目录失败", exc_info=True)

        original_log = getattr(scraper, "log", None)

        def _hooked(msg="", *args, **kwargs):
            _refresh_push(msg)
            try:
                if callable(original_log):
                    original_log(msg)
            except Exception:  # noqa: BLE001
                pass

        scraper.log = _hooked

        args = type("RefreshArgs", (), {})()
        args.force = bool(force)
        args.verify = False
        args.skip_battles = False
        args.sample_battles = 0
        args.workers = 3                    # 温和并发，避免给官方接口压力
        # v34：默认增量 —— 本地已抓过的对局直接跳过，只补新赛季 / 新场次，
        # 这样「只补缺的几个月」而不是每次都全量重跑（全量 5~10 分钟 → 增量通常几十秒）。
        args.incremental = not bool(force)
        args.full = False
        _refresh_push("开始增量抓取（本地已有对局会跳过；并发 3，请求间隔 0.5~1.5s）…")
        rc = scraper.Scraper(args).run()
        ok_flag = (rc == 0)
        if not ok_flag:
            err = "抓取脚本返回码 %s" % rc
    except Exception as exc:  # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, exc)
        app.logger.exception("[refresh] 抓取失败")
        _refresh_push("抓取失败：%s" % err)
    finally:
        # 数据变了，内存缓存必须让位
        _drop_data_caches()
        try:
            _kpl_rules_mod.reload_rules()
        except Exception:  # noqa: BLE001
            pass
        with _REFRESH_LOCK:
            _REFRESH_STATE["running"] = False
            _REFRESH_STATE["ok"] = bool(ok_flag)
            _REFRESH_STATE["error"] = err
            _REFRESH_STATE["finished_at"] = time.time()
        _refresh_push("完成" if ok_flag else ("结束（有错误）：%s" % err))


@app.route("/api/data/refresh", methods=["POST"])
def api_data_refresh():
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force"))
    with _REFRESH_LOCK:
        if _REFRESH_STATE.get("running"):
            raise APIError("已经有一个抓取任务在跑，请等它结束", 409)
        last = _REFRESH_STATE.get("finished_at") or 0
        wait = REFRESH_MIN_INTERVAL - (time.time() - last) if last else 0
        if wait > 0 and not force:
            raise APIError(
                "刚抓过不久，为避免被官方限流，请 %d 分钟后再试（或点「强制重抓」）" % int(wait / 60 + 0.5),
                429,
            )
        _REFRESH_STATE.update({
            "running": True,
            "started_at": time.time(),
            "finished_at": 0.0,
            "ok": None,
            "error": "",
            "phase": "准备中…",
            "log": [],
        })
    thread = threading.Thread(target=_refresh_worker, args=(force,), daemon=True,
                              name="kpl-data-refresh")
    thread.start()
    return ok({"started": True, "state": _refresh_snapshot()})


@app.route("/api/data/refresh/status", methods=["GET"])
def api_data_refresh_status():
    return ok({"state": _refresh_snapshot()})


# --------------------------------------------------------------------------- #
# v44：只刷新「王者荣耀官网」英雄数据（官方分路 + 压制/被压制关系）
# --------------------------------------------------------------------------- #
_HERO_REFRESH_STATE = {
    "running": False,
    "started_at": 0.0,
    "finished_at": 0.0,
    "ok": None,
    "error": "",
    "phase": "",
    "log": [],
}
_HERO_REFRESH_LOCK = threading.Lock()
HERO_REFRESH_MIN_INTERVAL = 30 * 60      # 与 KPL 数据同等的冷静期，避免被官方限流


def _hero_refresh_snapshot():
    with _HERO_REFRESH_LOCK:
        raw = dict(_HERO_REFRESH_STATE)
        raw["log"] = list(raw.get("log") or [])
    now = time.time()
    remain = 0
    if raw.get("finished_at"):
        remain = max(0, int(HERO_REFRESH_MIN_INTERVAL - (now - raw["finished_at"])))
    raw["cooldown"] = remain
    raw["min_interval"] = HERO_REFRESH_MIN_INTERVAL
    raw["next_allowed_in"] = remain
    return raw


def _hero_refresh_push(msg):
    line = str(msg or "").rstrip()
    if not line:
        return
    with _HERO_REFRESH_LOCK:
        logs = _HERO_REFRESH_STATE.setdefault("log", [])
        logs.append(line)
        if len(logs) > REFRESH_LOG_LIMIT:
            del logs[: len(logs) - REFRESH_LOG_LIMIT]
        _HERO_REFRESH_STATE["phase"] = line[:80]


def _hero_refresh_worker():
    ok_flag = False
    err = ""
    try:
        import importlib
        from pathlib import Path as _Path

        scraper = importlib.import_module("scraper")
        try:
            scraper.DATA_DIR = _Path(DATA_DIR)
            scraper.STATIC_IMG_HEROES = _Path(IMAGES_DIR) / "heroes"
            scraper.STATIC_IMG_TEAMS = _Path(IMAGES_DIR) / "teams"
            scraper.CACHE_DIR = _Path(DATA_DIR) / ".scrape_cache"
        except Exception:  # noqa: BLE001
            app.logger.warning("[hero-refresh] 重定向 scraper 目录失败", exc_info=True)

        original_log = getattr(scraper, "log", None)

        def _hooked(msg="", *args, **kwargs):
            _hero_refresh_push(msg)
            try:
                if callable(original_log):
                    original_log(msg)
            except Exception:  # noqa: BLE001
                pass

        scraper.log = _hooked
        _hero_refresh_push("开始抓取王者荣耀官网英雄数据（分路 + 压制关系）…")
        rc = scraper.refresh_hero_extra(progress=_hero_refresh_push)
        ok_flag = (rc == 0)
        if not ok_flag:
            err = "抓取脚本返回码 %s" % rc
    except Exception as exc:  # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, exc)
        app.logger.exception("[hero-refresh] 抓取失败")
        _hero_refresh_push("抓取失败：%s" % err)
    finally:
        _drop_data_caches()
        with _HERO_REFRESH_LOCK:
            _HERO_REFRESH_STATE["running"] = False
            _HERO_REFRESH_STATE["ok"] = bool(ok_flag)
            _HERO_REFRESH_STATE["error"] = err
            _HERO_REFRESH_STATE["finished_at"] = time.time()
        _hero_refresh_push("完成" if ok_flag else ("结束（有错误）：%s" % err))


@app.route("/api/data/refresh_hero", methods=["POST"])
def api_data_refresh_hero():
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force"))
    with _HERO_REFRESH_LOCK:
        if _HERO_REFRESH_STATE.get("running"):
            raise APIError("官网英雄数据正在更新，请等它结束", 409)
        last = _HERO_REFRESH_STATE.get("finished_at") or 0
        wait = HERO_REFRESH_MIN_INTERVAL - (time.time() - last) if last else 0
        if wait > 0 and not force:
            raise APIError(
                "刚更新过不久，为避免被官方限流，请 %d 分钟后再试（或点「强制重抓」）"
                % int(wait / 60 + 0.5),
                429,
            )
        _HERO_REFRESH_STATE.update({
            "running": True,
            "started_at": time.time(),
            "finished_at": 0.0,
            "ok": None,
            "error": "",
            "phase": "准备中…",
            "log": [],
        })
    threading.Thread(target=_hero_refresh_worker, daemon=True,
                     name="kpl-hero-refresh").start()
    return ok({"started": True, "state": _hero_refresh_snapshot()})


@app.route("/api/data/refresh_hero/status", methods=["GET"])
def api_data_refresh_hero_status():
    return ok({"state": _hero_refresh_snapshot(), "info": _hero_extra_info()})


def _warm_data_caches():
    """启动后台预热「更新数据」页要用的统计缓存。

    ``bp_history_by_league`` 有 8 个文件、3000+ 局，首次解析要 200~300ms；
    放到后台线程先算好，玩家打开「更新数据」页时就是毫秒级。
    """
    try:
        root = _data_root()
        for rel in ("hero_stats_by_league", "player_stats_by_league",
                    "team_stats_by_league", "bp_history_by_league"):
            _count_json_rows(root, rel)
        _hero_extra_info()
    except Exception:  # noqa: BLE001
        app.logger.debug("[warm] 统计缓存预热失败", exc_info=True)


def _warm_later():
    """v52：延迟 8 秒再预热。

    以前是导入即预热，会和启动后的第一批请求抢 CPU（2 核云服务器上首屏会明显卡一下）。
    现在先让首页/对局请求跑完，再在后台补算「更新数据」页要用的统计。
    """
    try:
        time.sleep(8.0)
    except Exception:  # noqa: BLE001
        pass
    _warm_data_caches()


threading.Thread(target=_warm_later, daemon=True, name="kpl-warm-cache").start()


def _drop_data_caches():
    """数据变动后清掉统计缓存 / 数据加载缓存。"""
    with _COUNT_CACHE_LOCK:
        _COUNT_CACHE.clear()
    try:
        data_loader.clear_cache()
    except Exception:  # noqa: BLE001
        pass


def _hero_extra_info():
    """官网英雄数据的现状（给「更新数据」页显示「已内置 / 缺多少」）。"""
    root = _data_root()
    out = {"relations_total": 0, "relations_available": 0, "relations_missing": 0,
           "relations_fetched_at": "", "relations_source": "",
           "official_lane_heroes": 0, "heroes_total": 0, "bundled": False}
    try:
        payload = json.loads(io.open(os.path.join(root, "hero_relations.json"),
                                     encoding="utf-8").read())
        rows = payload.get("heroes") or []
        out["relations_total"] = len(rows)
        out["relations_available"] = sum(1 for r in rows if r.get("counters") is not None)
        out["relations_missing"] = sum(1 for r in rows if r.get("counters") is None)
        out["relations_fetched_at"] = payload.get("_fetched_at") or ""
        out["relations_source"] = payload.get("_source") or ""
        out["bundled"] = True
    except Exception:                                        # noqa: BLE001
        pass
    try:
        heroes = json.loads(io.open(os.path.join(root, "heroes.json"),
                                    encoding="utf-8").read()).get("heroes") or []
        out["heroes_total"] = len(heroes)
        out["official_lane_heroes"] = sum(
            1 for h in heroes if h.get("lane_source") == "pvp_qq_official"
        )
    except Exception:                                        # noqa: BLE001
        pass
    return out


@app.route("/api/data/info", methods=["GET"])
def api_data_info():
    root = _data_root()

    def _read(name):
        try:
            return json.loads(io.open(os.path.join(root, name), encoding="utf-8").read())
        except Exception:                                    # noqa: BLE001
            return {}

    heroes = _read("heroes.json")
    leagues = _read("leagues.json")
    writable = True
    try:
        probe = os.path.join(root, ".write_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
    except Exception:                                        # noqa: BLE001
        writable = False
    leagues_rows = leagues.get("leagues") or []
    return ok({
        "data_dir": root,
        "writable": writable,
        "heroes": len(heroes.get("heroes") or []),
        "leagues": len(leagues_rows),
        "fetched_at": heroes.get("_fetched_at") or "",
        "league_ids": [str(r.get("league_id")) for r in leagues_rows],
        "league_names": {str(r.get("league_id")): str(r.get("name") or "") for r in leagues_rows},
        "hero_stats": _count_json_rows(root, "hero_stats_by_league"),
        "player_stats": _count_json_rows(root, "player_stats_by_league"),
        "team_stats": _count_json_rows(root, "team_stats_by_league"),
        "bp_history": _count_json_rows(root, "bp_history_by_league"),
        "import_items": list(_DATA_IMPORT_ITEMS),
        "hero_extra": _hero_extra_info(),
        "hero_refresh": _hero_refresh_snapshot(),
    })


def _pack_root(names):
    """数据包可能是 zip 根就是 data/，也可能多套一层（如 kpl-data/data/…）。"""
    for name in names:
        if name.endswith("heroes.json"):
            return name[: -len("heroes.json")]
    return ""


@app.route("/api/data/import", methods=["POST"])
def api_data_import():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise APIError("请选择数据包文件（.zip）", 400)
    if not upload.filename.lower().endswith(".zip"):
        raise APIError("数据包必须是 .zip 格式", 400)

    import shutil
    import tempfile
    import zipfile

    root = _data_root()
    tmpdir = tempfile.mkdtemp(prefix="kpl-data-")
    try:
        archive = os.path.join(tmpdir, "pack.zip")
        upload.save(archive)
        try:
            zf = zipfile.ZipFile(archive)
        except Exception:                                    # noqa: BLE001
            raise APIError("数据包损坏，无法解压", 400)
        names = zf.namelist()
        prefix = _pack_root(names)
        if not prefix:
            raise APIError("数据包里没有 heroes.json，不是有效的 KPL 数据包", 400)
        if not any(n.endswith(prefix + "leagues.json") for n in names):
            raise APIError("数据包里缺少 leagues.json", 400)

        work = os.path.join(tmpdir, "unpack")
        os.makedirs(work)
        zf.extractall(work)
        zf.close()
        src_root = os.path.join(work, prefix.replace("/", os.sep)) if prefix else work

        try:
            heroes = json.loads(io.open(os.path.join(src_root, "heroes.json"), encoding="utf-8").read())
        except Exception:                                    # noqa: BLE001
            raise APIError("数据包里的 heroes.json 无法解析", 400)
        hero_count = len(heroes.get("heroes") or [])
        if hero_count < 50:
            raise APIError("数据包里的英雄只有 %d 个，疑似不完整（至少 50 个）" % hero_count, 400)

        # 备份将被覆盖的条目（不动 data/tourney 存档、ai_config.json）
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(root, "data_backup_" + stamp)
        os.makedirs(backup, exist_ok=True)
        replaced = []
        for item in _DATA_IMPORT_ITEMS:
            src = os.path.join(src_root, item)
            if not os.path.exists(src):
                continue
            dst = os.path.join(root, item)
            if os.path.exists(dst):
                try:
                    if os.path.isdir(dst):
                        shutil.copytree(dst, os.path.join(backup, item))
                    else:
                        shutil.copy2(dst, os.path.join(backup, item))
                except Exception:                            # noqa: BLE001
                    pass
            if os.path.isdir(src):
                if os.path.isdir(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            replaced.append(item)

        if "heroes.json" not in replaced:
            raise APIError("数据包缺少 heroes.json", 400)

        # v33：数据包里如果带了头像 / 队徽（images/ 或 static/images/），一起更新到外部图片目录
        images_replaced = 0
        for cand in ("images", os.path.join("static", "images")):
            src_imgs = os.path.join(src_root, cand)
            if not os.path.isdir(src_imgs):
                continue
            dst_imgs = IMAGES_DIR
            os.makedirs(dst_imgs, exist_ok=True)
            try:
                shutil.copytree(dst_imgs, os.path.join(backup, "images"), dirs_exist_ok=True)
            except Exception:                                # noqa: BLE001
                pass
            images_replaced = _paths_mod._copy_missing(src_imgs, dst_imgs) if _paths_mod else 0
            if not images_replaced:  # 没有 _paths_mod 时直接覆盖式复制
                for cur, _dirs, files in os.walk(src_imgs):
                    rel = os.path.relpath(cur, src_imgs)
                    target = dst_imgs if rel == "." else os.path.join(dst_imgs, rel)
                    os.makedirs(target, exist_ok=True)
                    for name in files:
                        try:
                            shutil.copy2(os.path.join(cur, name), os.path.join(target, name))
                            images_replaced += 1
                        except Exception:                    # noqa: BLE001
                            pass
            break

        _drop_data_caches()
        try:
            _kpl_rules_mod.reload_rules()
        except Exception:                                    # noqa: BLE001
            pass

        leagues = json.loads(io.open(os.path.join(root, "leagues.json"), encoding="utf-8").read())
        app.logger.info("[data] 导入数据包 %s：英雄 %d，赛事 %d，替换 %s，备份 %s",
                        upload.filename, hero_count, len(leagues.get("leagues") or []),
                        ",".join(replaced), backup)
        return ok({
            "heroes": hero_count,
            "leagues": len(leagues.get("leagues") or []),
            "replaced": replaced,
            "backup": backup,
            "fetched_at": heroes.get("_fetched_at") or "",
        })
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route("/data-update", methods=["GET"])
def page_data_update():
    return _render_page("data_update.html")


if __name__ == "__main__":
    try:  # Windows 控制台中文输出
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # ---- v9 补丁：监听 IP / 端口来自 data/server_config.json（环境变量可覆盖）----
    if server_config is not None:
        _eff = server_config.effective()
        host = _eff["host"]
        port = _eff["port"]
        if _eff["port_from_env"]:
            _say("[配置] PORT 环境变量覆盖了配置文件：%d" % port)
        if _eff["host_from_env"]:
            _say("[配置] HOST 环境变量覆盖了配置文件：%s" % host)
    else:
        host = os.environ.get("HOST") or "0.0.0.0"
        try:
            port = int(os.environ.get("PORT", "5000") or 5000)
        except (TypeError, ValueError):
            _say("[警告] 环境变量 PORT 不是整数，回退到 5000")
            port = 5000

    SERVER_RUNTIME["host"] = host
    SERVER_RUNTIME["port"] = port

    startup_check()
    _say("")
    _say("KPL BP 模拟器后端已启动： http://%s:%d" % (host, port))
    if server_config is not None:
        _say("[配置] 监听地址 %s:%d（可在「设置 → 服务器访问地址」里改，改完需重启）"
             % (host, port))
        for _row in server_config.access_urls({"host": host, "port": port}):
            _say("       %-14s %s  —— %s" % (_row["label"], _row["url"], _row["note"]))
        if host not in ("0.0.0.0", "::", ""):
            _say("[配置] 当前只监听 %s，外部设备可能访问不到；服务器部署建议设成 0.0.0.0" % host)
    # ---- v10：打包成 exe 双击启动时，自动打开浏览器 ----
    if getattr(sys, "frozen", False):
        def _open_browser_later() -> None:
            import time
            import webbrowser

            time.sleep(1.6)
            url = "http://127.0.0.1:%d" % port
            try:
                ok = webbrowser.open(url)
                app.logger.info("[启动] 已请求打开浏览器 %s（webbrowser=%s）", url, ok)
            except Exception as exc:                       # noqa: BLE001
                app.logger.info("[启动] 自动打开浏览器失败（%s），请手动访问 %s", exc, url)

        threading.Thread(target=_open_browser_later, daemon=True).start()

    _say("")

    # ---- v10：端口被占用 / 已在运行 时给友好提示，避免窗口一闪而过 ----
    import webbrowser as _webbrowser

    if not _port_free(port):
        if _is_our_game(port):
            _url = "http://127.0.0.1:%d" % port
            _say("[提示] 端口 %d 上已经有一个本游戏在运行：%s" % (port, _url))
            _say("       已经为你打开那个窗口，本次不用再启动第二个。")
            try:
                _webbrowser.open(_url)
            except Exception:  # noqa: BLE001
                pass
            _pause_before_exit("游戏本来就在运行中（%s）。浏览器已经打开，关掉这个窗口即可。" % _url)
            sys.exit(0)
        _fallback = None
        for _p in range(port + 1, port + 21):
            if _port_free(_p):
                _fallback = _p
                break
        if _fallback is None:
            _pause_before_exit(
                "端口 %d 被别的程序占用，附近 20 个端口也都被占了；"
                "请在「设置 → 服务器访问地址」里换一个端口，或关掉占用该端口的程序后重试。" % port
            )
            sys.exit(1)
        _say("[提示] 端口 %d 被占用（不是本游戏），已自动改用 %d" % (port, _fallback))
        port = _fallback
        SERVER_RUNTIME["port"] = port

    # ---- v30：打开 HTTP/1.1 keep-alive ----
    # Werkzeug 开发服务器默认 HTTP/1.0（每个响应都 Connection: close），
    # 一个页面要加载几十个 css/js/图片请求，每次都要重新握手。
    # 切成 HTTP/1.1 后连接可复用（Flask 的正常响应都带 Content-Length，安全）。
    try:
        from werkzeug.serving import WSGIRequestHandler as _WzHandler
        if str(getattr(_WzHandler, "protocol_version", "")) != "HTTP/1.1":
            _WzHandler.protocol_version = "HTTP/1.1"
            _say("[性能] 已启用 HTTP/1.1 keep-alive（连接复用）")
    except Exception as _exc:                  # noqa: BLE001
        _say("[性能] keep-alive 开启失败（不影响使用）：%s" % _exc)

    # v52：云端部署用 waitress（纯 Python 生产级 WSGI，线程池 + 连接上限，
    # 不会像 werkzeug 开发服务器那样一个 keep-alive 连接常驻一个线程导致线程/内存上涨）。
    # 没装 waitress 时自动退回 werkzeug，本地体验不变。
    threads = int(os.environ.get("KPL_THREADS", "6") or 6)
    _waitress_serve = None
    try:
        from waitress import serve as _waitress_serve   # type: ignore[no-redef]
    except Exception:                          # noqa: BLE001
        _say("[服务] 未安装 waitress，使用 Flask 自带服务器（单机够用；云服务器建议 pip install waitress）")
    if _waitress_serve is not None:
        _say("[服务] waitress 线程池模式：%d 线程 / 连接上限 64" % max(2, threads))
        try:
            _waitress_serve(
                app,
                host=host,
                port=port,
                threads=max(2, threads),
                connection_limit=64,
                channel_timeout=60,
                cleanup_interval=30,
                ident=None,
            )
        except OSError as exc:
            _pause_before_exit(
                "启动失败：%s；最常见原因是端口 %d 已被占用（比如游戏已经开着一个窗口）；"
                "请先关掉之前的黑色窗口，或在「设置 → 服务器访问地址」里换端口。" % (exc, port)
            )
            sys.exit(1)
        sys.exit(0)
    try:
        app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
    except OSError as exc:                     # 端口在检测后被抢占等
        _pause_before_exit(
            "启动失败：%s；最常见原因是端口 %d 已被占用（比如游戏已经开着一个窗口）；"
            "请先关掉之前的黑色窗口，或在「设置 → 服务器访问地址」里换端口。" % (exc, port)
        )
        sys.exit(1)
    except Exception as exc:                    # noqa: BLE001 兜底，绝不让窗口闪退
        import traceback

        traceback.print_exc()
        _pause_before_exit("运行时出现异常：%s；详细信息见上面的日志。" % exc)
        sys.exit(1)
