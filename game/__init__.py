# -*- coding: utf-8 -*-
"""
KPL BP 小游戏 —— ``game`` 包。

四个模块的分工（与 ``docs/DATA_CONTRACT.md`` 一一对应）：

* :mod:`game.data_loader` —— 唯一的数据入口（``data/*.json`` → Python 结构），
  并把单赛季的全部数据 + 派生统计打包成 :class:`game.data_loader.LeagueContext`。
* :mod:`game.bp_engine`   —— BP 规则引擎（契约 §3 / §4）：步骤表、合法性校验、
  全局 BP、决胜局、AI 代走、``available`` 重算。
* :mod:`game.ai_coach`    —— AI 决策（契约 §5）：三档难度 + 权重转移。
* :mod:`game.scorer`      —— 阵容评分（契约 §6）与推荐文案。

这里把最常用的入口再导出一次，方便 ``from game import new_match``。
"""

from __future__ import annotations

from . import ai_coach, bp_engine, data_loader, scorer
from .ai_coach import AICoachError, choose_action
from .ai_coach import choose_side as ai_choose_side
from .bp_engine import (
    BP_STEPS,
    TIEBREAK_STEPS,
    BPError,
    apply_action,
    auto_advance,
    choose_side,
    legal_actions,
    new_match,
    next_game,
    pick_default_teams,
    refresh_match,
    resolve_teams,
    team_wins,
)
from .data_loader import (
    DATA_DIR,
    LANES,
    LeagueContext,
    hero_index,
    hero_list,
    league_context,
    league_meta,
    leagues_payload,
    load_bp_history,
    load_hero_stats,
    load_heroes,
    load_leagues,
    load_player_stats,
    load_team_meta,
    load_team_stats,
)
from .scorer import recommend_candidates, score_lineup, score_match

__all__ = [
    # 子模块
    "ai_coach",
    "bp_engine",
    "data_loader",
    "scorer",
    # 异常
    "AICoachError",
    "BPError",
    # 数据
    "DATA_DIR",
    "LANES",
    "LeagueContext",
    "clear_cache",
    "hero_index",
    "hero_list",
    "league_context",
    "league_meta",
    "leagues_payload",
    "load_bp_history",
    "load_hero_stats",
    "load_heroes",
    "load_leagues",
    "load_player_stats",
    "load_team_meta",
    "load_team_stats",
    # BP
    "BP_STEPS",
    "TIEBREAK_STEPS",
    "apply_action",
    "auto_advance",
    "choose_side",
    "legal_actions",
    "new_match",
    "next_game",
    "pick_default_teams",
    "refresh_match",
    "resolve_teams",
    "team_wins",
    # AI / 评分
    "ai_choose_side",
    "choose_action",
    "recommend_candidates",
    "score_lineup",
    "score_match",
]


def clear_cache() -> None:
    """清空 :mod:`game.data_loader` 的文件与上下文缓存（测试/热更新用）。"""
    data_loader.clear_cache()
