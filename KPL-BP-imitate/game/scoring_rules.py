# -*- coding: utf-8 -*-
"""阵容评分细则（可调参数）。

评分公式在 ``game/scorer.py``，本模块只负责把「参数」读出来 / 存下去 / 应用到评分器：

* 参数文件：``data/scoring_rules.json``（首次运行若不存在，用默认值生成；
  改坏了也能用「恢复默认」一键复原，绝不会把游戏卡死）
* 应用方式：``apply_rules()`` 直接改 ``game.scorer`` 里的模块级变量，
  评分、AI 决策、赛季页的「阵容评分」按钮会立即用新参数，不用重启。

参数一览
--------
``weights``                        五项基准权重（整数，总和必须 = 100）
    system    体系完整度     30   前排 / 输出 / 开团 / 续航是否齐全
    counter   对位克制       25   官方「压制 / 被压制」关系（pvp.qq.com）算净优势，无数据时转移权重
    version   版本强度       20   该赛季 KPL 真实胜率 min–max 线性映射
    lane      分路覆盖       15   5 个 pick 与该赛季真实 lane_list 的最优匹配
    weakness  阵容短板       10   1 −（分路缺口 + 胜率明显偏低的英雄）

``lane_primary``                   1.0    命中英雄主分路的得分系数（0~1.5）
``lane_secondary``                 0.75   只命中副分路的得分系数（0~1.5）
``lane_secondary_discount``        0.85   副分路整体折算比例（0~1.5）
``weakness_win_rate_gap``          1.5    胜率低于同分路中位数多少个百分点算「短板」（0~20）

权重转移
--------
某分项没有真实数据支撑时该项记 0，其权重按其余项的基准权重比例转移，
公式：``有效权重_y = 基准权重_y × 100 / Σ(有数据项的基准权重)``。
``counter`` 的数据来自王者荣耀官网英雄详情页的「英雄关系」（压制 / 被压制），
由 ``scraper.py`` 抓成 ``data/hero_relations.json``；某英雄还没官网详情页时该项记 0 并转移权重。
"""

from __future__ import annotations

import io
import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULTS",
    "PARAM_META",
    "load_rules",
    "save_rules",
    "reset_rules",
    "apply_rules",
    "state",
    "rules_path",
]

WEIGHT_KEYS = ("system", "counter", "version", "lane", "weakness")

DEFAULTS: Dict[str, Any] = {
    "weights": {"system": 30, "counter": 25, "version": 20, "lane": 15, "weakness": 10},
    "lane_primary": 1.0,
    "lane_secondary": 0.75,
    "lane_secondary_discount": 0.85,
    "weakness_win_rate_gap": 1.5,
}

#: 给前端 / 文档用的参数说明（顺序即展示顺序）
PARAM_META: List[Dict[str, Any]] = [
    {
        "key": "weights.system",
        "label": "体系完整度权重",
        "default": 30,
        "min": 0, "max": 100, "step": 1, "unit": "分",
        "desc": "前排 / 输出 / 开团 / 续航是否齐全。五项权重之和必须等于 100。",
    },
    {
        "key": "weights.counter",
        "label": "对位克制权重",
        "default": 25,
        "min": 0, "max": 100, "step": 1, "unit": "分",
        "desc": "用王者荣耀官网英雄详情页的「压制 / 被压制」关系算双方阵容净优势（我方压制 +1、被压制 -1，逐对累加后放大 3 倍并夹到 0~100）。没有官方数据的英雄该项记 0 分，权重按比例转移给其它项。",
    },
    {
        "key": "weights.version",
        "label": "版本强度权重",
        "default": 20,
        "min": 0, "max": 100, "step": 1, "unit": "分",
        "desc": "用该赛季 KPL 真实胜率做 min–max 线性映射，没有赛季数据时该项为 0。",
    },
    {
        "key": "weights.lane",
        "label": "分路覆盖权重",
        "default": 15,
        "min": 0, "max": 100, "step": 1, "unit": "分",
        "desc": "5 个 pick 与该赛季真实 lane_list 做最优匹配；全部英雄都没有分路数据时该项为 0。",
    },
    {
        "key": "weights.weakness",
        "label": "阵容短板权重",
        "default": 10,
        "min": 0, "max": 100, "step": 1, "unit": "分",
        "desc": "1 − 短板程度；短板来自分路缺口与所选英雄胜率明显低于同分路中位数。",
    },
    {
        "key": "lane_primary",
        "label": "主分路命中系数",
        "default": 1.0,
        "min": 0, "max": 1.5, "step": 0.05, "unit": "",
        "desc": "英雄打在自己最常走的分路上时的得分系数，1.0 = 满分。",
    },
    {
        "key": "lane_secondary",
        "label": "副分路命中系数",
        "default": 0.75,
        "min": 0, "max": 1.5, "step": 0.05, "unit": "",
        "desc": "命中英雄的副分路（不是主分路）时的得分系数。",
    },
    {
        "key": "lane_secondary_discount",
        "label": "副分路折算比例",
        "default": 0.85,
        "min": 0, "max": 1.5, "step": 0.05, "unit": "",
        "desc": "副分路命中后在「分路契合」里的整体折算比例（越大对副分路越宽容）。",
    },
    {
        "key": "weakness_win_rate_gap",
        "label": "短板判定阈值",
        "default": 1.5,
        "min": 0, "max": 20, "step": 0.1, "unit": "个百分点",
        "desc": "所选英雄胜率低于同分路中位数超过这个数值时，计为「阵容短板」。",
    },
]


def _base_dir() -> str:
    try:
        from . import paths  # 打包后 data/ 在 exe 同级
        return paths.data_dir()
    except Exception:                                   # noqa: BLE001
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def rules_path() -> str:
    return os.path.join(_base_dir(), "scoring_rules.json")


_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {"mtime": None, "rules": None}


def _normalize(raw: Any) -> Dict[str, Any]:
    """把外部 JSON 收敛成合法参数：越界裁剪、缺项补默认，权重和强制 = 100。"""
    data = json.loads(json.dumps(DEFAULTS))             # 深拷贝
    if not isinstance(raw, dict):
        return data
    weights = raw.get("weights")
    if isinstance(weights, dict):
        for key in WEIGHT_KEYS:
            try:
                value = int(round(float(weights.get(key, data["weights"][key]))))
            except (TypeError, ValueError):
                continue
            data["weights"][key] = max(0, min(100, value))
        total = sum(data["weights"].values())
        if total <= 0:
            data["weights"] = dict(DEFAULTS["weights"])
        elif total != 100:
            # 按比例缩放，余数补给「体系完整度」，保证 Σ = 100
            scaled = {k: max(0, int(round(v * 100.0 / total))) for k, v in data["weights"].items()}
            diff = 100 - sum(scaled.values())
            scaled["system"] = max(0, scaled["system"] + diff)
            data["weights"] = scaled
    for key, default in (("lane_primary", 1.0), ("lane_secondary", 0.75),
                         ("lane_secondary_discount", 0.85), ("weakness_win_rate_gap", 1.5)):
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        if key == "weakness_win_rate_gap":
            data[key] = round(max(0.0, min(20.0, value)), 2)
        else:
            data[key] = round(max(0.0, min(1.5, value)), 3)
    return data


def load_rules() -> Dict[str, Any]:
    """读参数文件（按 mtime 缓存）；文件损坏 / 缺失时静默回退默认值。"""
    path = rules_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _normalize(None)
    with _LOCK:
        if _CACHE["rules"] is not None and _CACHE["mtime"] == mtime:
            return json.loads(json.dumps(_CACHE["rules"]))
    raw: Any = None
    try:
        with io.open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:                                   # noqa: BLE001
        raw = None
    rules = _normalize(raw)
    with _LOCK:
        _CACHE["mtime"] = mtime
        _CACHE["rules"] = rules
    return json.loads(json.dumps(rules))


def save_rules(patch: Any) -> Dict[str, Any]:
    """保存（合并 patch → 规范化 → 原子落盘 → 立即生效）。"""
    current = load_rules()
    if isinstance(patch, dict):
        weights = patch.get("weights")
        if isinstance(weights, dict):
            for key in WEIGHT_KEYS:
                if key in weights:
                    current["weights"][key] = weights[key]
        for key in ("lane_primary", "lane_secondary", "lane_secondary_discount",
                    "weakness_win_rate_gap"):
            if key in patch:
                current[key] = patch[key]
    rules = _normalize(current)
    rules["_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = rules_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rules, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with _LOCK:
        _CACHE["mtime"] = None
        _CACHE["rules"] = None
    apply_rules(rules)
    return rules


def reset_rules() -> Dict[str, Any]:
    """恢复默认评分细则。"""
    rules = _normalize(None)
    return save_rules(rules)


def apply_rules(rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把参数写进 ``game.scorer`` 的模块级变量。失败只打日志，绝不影响游戏。"""
    rules = rules or load_rules()
    try:
        from . import scorer
        weights = dict(rules.get("weights") or DEFAULTS["weights"])
        scorer.BASE_WEIGHTS.clear()
        scorer.BASE_WEIGHTS.update({k: int(weights.get(k, DEFAULTS["weights"][k])) for k in WEIGHT_KEYS})
        scorer._LANE_PRIMARY = float(rules.get("lane_primary", 1.0))
        scorer._LANE_SECONDARY = float(rules.get("lane_secondary", 0.75))
        scorer._SECONDARY_DISCOUNT = float(rules.get("lane_secondary_discount", 0.85))
        scorer._WEAKNESS_WIN_RATE_GAP = float(rules.get("weakness_win_rate_gap", 1.5))
    except Exception as exc:                            # noqa: BLE001
        print("[scoring] 应用评分细则失败（用默认值继续）：%s" % exc)
    return rules


def state() -> Dict[str, Any]:
    """给设置页的完整状态：当前值 / 默认值 / 参数说明 / 文件路径。"""
    return {
        "rules": load_rules(),
        "defaults": json.loads(json.dumps(DEFAULTS)),
        "meta": PARAM_META,
        "path": rules_path(),
    }
