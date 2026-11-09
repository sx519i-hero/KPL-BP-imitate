#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
KPL 模拟 BP 教练小游戏 —— 真实数据采集脚本
=============================================

数据来源（全部为腾讯王者荣耀官方公开接口，已实测可用）：
  * 赛事列表   https://prod.comp.smoba.qq.com/leaguesite/leagues/open
  * 英雄榜     https://prod.comp.smoba.qq.com/leaguesite/league/hero/settle_list/open?league_id=
  * 选手榜     https://prod.comp.smoba.qq.com/leaguesite/league/player/settle_list/open?league_id=
  * 战队榜     https://prod.comp.smoba.qq.com/leaguesite/league/team/settle_list/open?league_id=
  * 赛程列表   https://prod.comp.smoba.qq.com/leaguesite/matches/open?league_id=
  * 小局列表   https://prod.comp.smoba.qq.com/leaguesite/match/battles/open?match_id=
  * 小局详情   https://prod.comp.smoba.qq.com/leaguesite/battle/open?battle_id=   (含完整 bp_list)
  * 英雄总表   https://pvp.qq.com/web201605/js/herolist.json
  * 英雄头像   https://game.gtimg.cn/images/yxzj/img201606/heroimg/{ename}/{ename}.jpg

设计原则：
  1. **绝不编造任何数值**。接口没给的字段一律写 null，缺失的赛季写空数组 + `_missing: true`。
  2. 全量抓取：2025 年起全部 KPL 赛事的**每一场**对局的**每一小局** BP 序列。
  3. 断点续抓：所有 API 响应缓存在 `.scrape_cache/`，重复运行秒级完成；`--force` 强制重抓。
  4. 抓取礼仪：随机延时 0.5~1.5s、标准 UA、失败重试退避。

用法：
  python scraper.py              # 增量抓取（有缓存则很快）
  python scraper.py --force      # 忽略缓存，全量重抓
  python scraper.py --verify     # 只检查已有数据完整性，不联网
  python scraper.py --skip-battles   # 快速模式：不抓 BP 明细（只抓榜单）
  python scraper.py --workers 5  # 并发数（默认 5）
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import random
import re as re_mod
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# 可选依赖：优先 requests，退回标准库 urllib（保证零依赖也能跑）
# --------------------------------------------------------------------------- #
try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None

try:
    import urllib.request
    import urllib.error
except Exception:  # pragma: no cover
    urllib = None


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
API_BASE = "https://prod.comp.smoba.qq.com"
URL_LEAGUES = API_BASE + "/leaguesite/leagues/open"
URL_HERO_SETTLE = API_BASE + "/leaguesite/league/hero/settle_list/open"
URL_PLAYER_SETTLE = API_BASE + "/leaguesite/league/player/settle_list/open"
URL_TEAM_SETTLE = API_BASE + "/leaguesite/league/team/settle_list/open"
URL_MATCHES = API_BASE + "/leaguesite/matches/open"
URL_BATTLES = API_BASE + "/leaguesite/match/battles/open"
URL_BATTLE_DETAIL = API_BASE + "/leaguesite/battle/open"
URL_HEROLIST = "https://pvp.qq.com/web201605/js/herolist.json"
URL_HERO_AVATAR = "https://game.gtimg.cn/images/yxzj/img201606/heroimg/{ename}/{ename}.jpg"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://pvp.qq.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

MIN_YEAR = 2025          # 只抓 2025 年（含）之后的赛事
DELAY_MIN = 0.5          # 请求间隔下限（秒）
DELAY_MAX = 1.5          # 请求间隔上限（秒）
MAX_RETRY = 3
RETRY_BACKOFF = 2.0

# 官方 hero_type -> 粗糙定位（**v2 起不再用于分路判定**）
# 用户明确要求：herolist.json 自带的分路标签粗糙且常错（关羽被标坦克、孙膑被标辅助等），
# 因此 v2 的 lane / lane_list 一律来自 KPL 实战 battle_player_list[].position_desc。
# 这份映射只在拿不到任何实战分路数据时，作为 heroes.json 里 `hero_type` 的附带说明保留，
# **绝不写进 lane_list**。
HERO_TYPE_TO_LANE = {
    1: "对抗路",   # 战士
    2: "中路",     # 法师
    3: "对抗路",   # 坦克
    4: "打野",     # 刺客
    5: "发育路",   # 射手
    6: "游走",     # 辅助
}
Lanes = ["对抗路", "打野", "中路", "发育路", "游走"]

# v43：官方 herolist.json 的 ``roles`` / ``extra_cold_lane`` 用的是**分路 code**，
# 与英雄定位 hero_type 是两套完全不同的编码，以前混用导致「没在 KPL 出场过的英雄默认分路错」。
#   1=对抗路 2=打野 3=中路 4=发育路 5=游走   （用 133 个英雄里 KPL 有分路数据的那批做相关性验证过）
OFFICIAL_LANE_CODE = {1: "对抗路", 2: "打野", 3: "中路", 4: "发育路", 5: "游走"}

# 官方英雄详情页（GBK）：英雄关系里带「压制英雄 / 被压制英雄」，是对位克制的官方来源
URL_HERO_DETAIL = "https://pvp.qq.com/web201605/herodetail/{ename}.shtml"
# 新英雄（影 / 卢雅那 / 元流之子…）官网详情页用拼音 id_name 而不是数字 ename，
# 直接按 ename 访问会 404 —— 必须再按 id_name 试一次。
URL_HERO_DETAIL_BY_NAME = "https://pvp.qq.com/web201605/herodetail/{id_name}.shtml"

LANE_ORDER = {name: idx for idx, name in enumerate(Lanes)}

# KPL 实战 position_desc 的合法取值（实测：2=中路 4=游走 5=打野 6=对抗路 7=发育路）
# v2 权威数据源；任何不在这个集合里的值都丢弃，不猜。
VALID_LANES = set(Lanes)


def lane_sort_key(counts):
    """把 {分路: 局数} 转成有序分路数组：先按局数降序，再按 Lanes 固定序。"""
    items = [(lane, cnt) for lane, cnt in (counts or {}).items() if lane in VALID_LANES]
    items.sort(key=lambda kv: (-kv[1], LANE_ORDER.get(kv[0], 99)))
    return [lane for lane, _ in items]

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
STATIC_IMG_HEROES = PROJECT_ROOT / "static" / "images" / "heroes"
STATIC_IMG_TEAMS = PROJECT_ROOT / "static" / "images" / "teams"
CACHE_DIR = PROJECT_ROOT / ".scrape_cache"

TODAY = date.today().isoformat()

# 进度输出锁
_print_lock = threading.Lock()


def log(msg: str = "") -> None:
    with _print_lock:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()


def log_inline(msg: str) -> None:
    with _print_lock:
        sys.stdout.write("\r" + msg)
        sys.stdout.flush()


# --------------------------------------------------------------------------- #
# HTTP 客户端（带磁盘缓存 + 随机延时 + 重试）
# --------------------------------------------------------------------------- #
class HttpClient:
    """线程安全的抓取客户端。

    * 每个工作线程持有独立 Session（requests.Session 不是线程安全的）。
    * 命中缓存时不发请求、不 sleep —— 所以重复运行几乎瞬时完成。
    """

    def __init__(self, cache_dir: Path, use_cache: bool = True, delay=(DELAY_MIN, DELAY_MAX)):
        self.cache_dir = cache_dir
        self.use_cache = use_cache
        self.delay = delay
        self._local = threading.local()
        self.stats = Counter()
        self._stats_lock = threading.Lock()

    # -- session ---------------------------------------------------------- #
    def _session(self):
        if requests is None:
            return None
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            self._local.session = s
        return s

    # -- cache ------------------------------------------------------------ #
    def _cache_path(self, key: str) -> Path:
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.cache_dir / "http" / h[:2] / (h + ".gz")

    def _cache_read(self, key: str):
        if not self.use_cache:
            return None
        p = self._cache_path(key)
        if not p.exists():
            return None
        try:
            with gzip.open(p, "rb") as f:
                return f.read()
        except Exception:
            return None

    def _cache_write(self, key: str, raw: bytes) -> None:
        if not self.use_cache:
            return
        p = self._cache_path(key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with gzip.open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, p)
        except Exception:
            pass

    # -- raw fetch -------------------------------------------------------- #
    def fetch_bytes(self, url: str, retries: int = MAX_RETRY) -> bytes:
        cached = self._cache_read("GET " + url)
        if cached is not None:
            self._bump("cache_hit")
            # 缓存命中也做一点点延时，避免把上游打挂（但小得多）
            return cached

        last_err = None
        for attempt in range(retries):
            # 抓取礼仪：随机延时
            time.sleep(random.uniform(*self.delay))
            try:
                sess = self._session()
                if sess is not None:
                    r = sess.get(url, timeout=30)
                    if r.status_code != 200:
                        raise IOError("HTTP %s" % r.status_code)
                    raw = r.content
                else:
                    req = urllib.request.Request(url, headers=HEADERS)
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        raw = resp.read()
                        if resp.headers.get("Content-Encoding") == "gzip":
                            raw = gzip.decompress(raw)
                self._cache_write("GET " + url, raw)
                self._bump("fetch")
                return raw
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < retries - 1:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
        self._bump("error")
        raise IOError("请求失败 %s: %r" % (url, last_err))

    def fetch_json(self, url: str) -> dict:
        raw = self.fetch_bytes(url)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            # 少数接口返回带 BOM 或 GBK
            try:
                return json.loads(raw.decode("utf-8-sig"))
            except Exception:
                return json.loads(raw.decode("gbk", "replace"))

    # -- stats ------------------------------------------------------------ #
    def _bump(self, k: str) -> None:
        with self._stats_lock:
            self.stats[k] += 1

    def download_file(self, url: str, dest: Path) -> bool:
        """下载二进制文件（头像/logo）。已存在且大小合理则跳过。"""
        try:
            if dest.exists() and dest.stat().st_size > 512:
                self._bump("file_skip")
                return True
            raw = self.fetch_bytes(url, retries=2)
            if len(raw) < 512:
                self._bump("file_small")
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, dest)
            self._bump("file_ok")
            return True
        except Exception:
            self._bump("file_fail")
            return False


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def api_data(obj: dict):
    """统一取接口的 data / results 字段，失败返回空。"""
    if not isinstance(obj, dict):
        return []
    if obj.get("code") not in (200, "200"):
        return []
    for key in ("data", "results"):
        if key in obj and obj[key] is not None:
            return obj[key]
    return []


def pct(v, digits: int = 2):
    """0~1 的比例 -> 0~100 的百分比；None 保持 None。"""
    if v is None:
        return None
    try:
        return round(float(v) * 100.0, digits)
    except (TypeError, ValueError):
        return None


def num(v, digits: int = 4):
    if v is None:
        return None
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def as_int(v):
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def d10(s):
    """'2026-06-17 14:00:00' -> '2026-06-17'"""
    if not s or not isinstance(s, str):
        return None
    return s.split(" ")[0]


def status_of(raw):
    """联赛 API status: 2=已结束, 1=进行中, 其它=未开始"""
    if raw == 2 or raw == "2":
        return "ended"
    if raw == 1 or raw == "1":
        return "ongoing"
    return "upcoming"


def mode_of(values):
    if not values:
        return None
    return Counter(values).most_common(1)[0][0]


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_json(path: Path):
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 采集器
# --------------------------------------------------------------------------- #
class Scraper:
    def __init__(self, args):
        self.args = args
        self.http = HttpClient(CACHE_DIR, use_cache=not args.force)
        self.leagues = []            # 契约 §1.1
        self.heroes = []             # 契约 §1.2
        self.teams_meta = {}         # team_id -> 契约 §1.3 项
        self.legacy_warnings = []

    # ------------------------------------------------------------------ #
    # 0. 目录
    # ------------------------------------------------------------------ #
    def ensure_dirs(self):
        for d in [
            DATA_DIR,
            DATA_DIR / "hero_stats_by_league",
            DATA_DIR / "player_stats_by_league",
            DATA_DIR / "team_stats_by_league",
            DATA_DIR / "bp_history_by_league",
            STATIC_IMG_HEROES,
            STATIC_IMG_TEAMS,
        ]:
            d.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 1. 英雄总表 + 头像
    # ------------------------------------------------------------------ #
    def step_heroes(self):
        log("【1/5】抓取全英雄总表 …")
        try:
            raw = self.http.fetch_json(URL_HEROLIST)
        except Exception as e:
            log("  ✗ 英雄总表抓取失败：%r" % (e,))
            raw = []
        if isinstance(raw, dict):
            raw = raw.get("heroes") or raw.get("data") or []

        heroes = []
        for h in raw:
            if not isinstance(h, dict):
                continue
            ename = as_int(h.get("ename"))
            cname = h.get("cname")
            if not ename or not cname:
                continue
            htype = as_int(h.get("hero_type"))
            # v2：lane / lane_list 只能来自 KPL 实战分路，这里先留空，
            # 由 collect_league_battles() 统计完后在 finalize_hero_lanes() 里回填。
            # role 字段保留 herolist 的原始定位（v1 已有字段，别处可能在用）。
            # v43：roles 是官方「分路」code（1 对抗路 / 2 打野 / 3 中路 / 4 发育路 / 5 游走），
            # 不能再用 hero_type 的映射去解（以前就是这么错的）。
            lane_official = []
            for tok in str(h.get("roles") or "").split("|"):
                ln2 = OFFICIAL_LANE_CODE.get(as_int(tok))
                if ln2 and ln2 not in lane_official:
                    lane_official.append(ln2)
            for tok in str(h.get("extra_cold_lane") or "").split("|"):
                ln2 = OFFICIAL_LANE_CODE.get(as_int(tok))
                if ln2 and ln2 not in lane_official:
                    lane_official.append(ln2)
            roles = list(lane_official)
            heroes.append(
                {
                    "ename": ename,
                    "cname": cname,
                    "title": h.get("title") or None,
                    "id_name": h.get("id_name") or None,
                    "hero_type": htype,
                    "role": roles[:2] if len(roles) > 1 else roles,
                    # v43：官方分路（roles + extra_cold_lane），KPL 没有实战数据时用它兜底
                    "lane_official": lane_official,
                    # 待 KPL 实战分路统计回填；没有实战数据时回退官方分路
                    "lane": None,
                    "lane_list": [],
                    "lane_source": "kpl_battle_position",
                    "lane_leagues": 0,
                    "avatar_path": "/static/images/heroes/%d.jpg" % ename,
                    "avatar_ok": False,
                }
            )

        heroes.sort(key=lambda x: x["ename"])
        self.heroes = heroes
        log("  英雄总数：%d" % len(heroes))

        if not heroes:
            log("  ✗ 没有拿到任何英雄，后续步骤将跳过头像下载")
            return

        # 下载头像
        log("  下载英雄头像到 static/images/heroes/ …")
        total = len(heroes)
        done = 0
        ok = 0
        lock = threading.Lock()

        def work(hero):
            dest = STATIC_IMG_HEROES / ("%d.jpg" % hero["ename"])
            return hero, self.http.download_file(
                URL_HERO_AVATAR.format(ename=hero["ename"]), dest
            )

        with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            futures = [pool.submit(work, h) for h in heroes]
            for fut in as_completed(futures):
                try:
                    hero, good = fut.result()
                except Exception:
                    continue
                with lock:
                    done += 1
                    if good:
                        hero["avatar_ok"] = True
                        ok += 1
                    if done % 10 == 0 or done == total:
                        log_inline("  头像 %d/%d（成功 %d）" % (done, total, ok))
        log("")

        write_json(
            DATA_DIR / "heroes.json",
            {"heroes": heroes, "_fetched_at": TODAY},
        )
        log("  ✓ 写入 data/heroes.json（%d 个英雄，%d 张头像）" % (len(heroes), ok))

    # ------------------------------------------------------------------ #
    # 2. 赛事枚举
    # ------------------------------------------------------------------ #
    def step_leagues(self):
        log("【2/5】枚举 2025 年起全部 KPL 赛事 …")
        try:
            obj = self.http.fetch_json(URL_LEAGUES)
        except Exception as e:
            log("  ✗ 赛事列表抓取失败：%r" % (e,))
            obj = {}
        results = api_data(obj)

        picked = []
        for lg in results:
            if not isinstance(lg, dict):
                continue
            year = as_int(lg.get("year"))
            ltype = (lg.get("league_type_name") or "").strip().lower()
            if year is None or year < MIN_YEAR:
                continue
            if ltype and ltype != "kpl":
                continue
            lid = str(lg.get("league_id") or "").strip()
            name = (lg.get("league_name") or "").strip()
            if not lid or not name:
                continue
            picked.append(
                {
                    "league_id": lid,
                    "name": name,
                    "year": year,
                    "season": as_int(lg.get("season")),
                    "league_type_name": lg.get("league_type_name") or None,
                    "start": d10(lg.get("start_time")),
                    "end": d10(lg.get("end_time")),
                    "status": status_of(lg.get("status")),
                    "icon": lg.get("league_icon") or None,
                    "match_count": None,   # 稍后填
                }
            )

        # 去重（同 league_id）
        seen = set()
        uniq = []
        for lg in picked:
            if lg["league_id"] in seen:
                continue
            seen.add(lg["league_id"])
            uniq.append(lg)
        uniq.sort(key=lambda x: (x.get("start") or "9999", x["league_id"]))

        self.leagues = uniq
        log("  枚举到 %d 个赛事：" % len(uniq))
        for lg in uniq:
            log("    %-10s %-22s %s ~ %s  [%s]"
                % (lg["league_id"], lg["name"], lg["start"], lg["end"], lg["status"]))

        if len(uniq) < 6:
            log("  ! 警告：只枚举到 %d 个赛事（目标 8 个）" % len(uniq))

    def default_league_id(self):
        ended = [lg for lg in self.leagues if lg["status"] == "ended"]
        if ended:
            return ended[-1]["league_id"]
        if self.leagues:
            return self.leagues[-1]["league_id"]
        return None

    # ------------------------------------------------------------------ #
    # 3. 单个赛季：榜单 + 战队元信息
    # ------------------------------------------------------------------ #
    def collect_league_boards(self, lg):
        """抓该赛季 英雄/选手/战队 三个榜单，返回 (hero_payload, player_payload, team_payload)。"""
        lid = lg["league_id"]
        name = lg["name"]

        # ---- 英雄榜 ----
        heroes_out = []
        try:
            obj = self.http.fetch_json("%s?league_id=%s" % (URL_HERO_SETTLE, lid))
            for row in api_data(obj):
                if not isinstance(row, dict):
                    continue
                info = row.get("hero_info") or {}
                st = row.get("statistics_info") or {}
                bp = row.get("bp_statistics_info") or {}
                ename = as_int(info.get("hero_id"))
                if not ename:
                    continue
                heroes_out.append(
                    {
                        "ename": ename,
                        "cname": info.get("hero_name") or None,
                        "pick_count": as_int(bp.get("pick_num")),
                        "ban_count": as_int(bp.get("ban_num")),
                        "pick_rate": pct(bp.get("pick_rate")),
                        "ban_rate": pct(bp.get("ban_rate")),
                        "win_count": as_int(st.get("victory_battle_count")),
                        "game_count": as_int(st.get("battle_count")),
                        "win_rate": pct(st.get("win_rate")),
                        "kda": num(st.get("avg_kda")),
                        "avg_kill": num(st.get("avg_kill_num")),
                        "avg_death": num(st.get("avg_death_num")),
                        "avg_assist": num(st.get("avg_assist_num")),
                    }
                )
        except Exception as e:
            log("    ! %s 英雄榜失败：%r" % (name, e))
        heroes_out.sort(key=lambda x: (-(x["win_rate"] or -1), x["ename"]))

        # ---- 选手榜 ----
        players_out = []
        try:
            obj = self.http.fetch_json("%s?league_id=%s" % (URL_PLAYER_SETTLE, lid))
            for row in api_data(obj):
                if not isinstance(row, dict):
                    continue
                info = row.get("player_info") or {}
                st = row.get("statistics_info") or {}
                pname = (info.get("player_name") or "").strip()
                if not pname:
                    continue
                pos = info.get("position_desc") or None
                if pos not in Lanes:
                    pos = None
                players_out.append(
                    {
                        "name": pname,
                        "team": info.get("team_name") or None,
                        "team_id": str(info.get("team_id") or "") or None,
                        "position": pos,
                        "matches": as_int(st.get("battle_count")),
                        "wins": as_int(st.get("victory_battle_count")),
                        "losses": as_int(st.get("defeated_battle_count")),
                        "win_rate": pct(st.get("win_rate")),
                        "kda": num(st.get("avg_kda")),
                        "gpm": num(st.get("avg_gpm"), 1),
                        "gold_rate": pct(st.get("avg_gold_rate")),
                        "damage_share": pct(st.get("avg_hurt_to_hero_total_rate")),
                        "participation_rate": num(st.get("avg_participation_rate"), 2),
                    }
                )
        except Exception as e:
            log("    ! %s 选手榜失败：%r" % (name, e))
        players_out.sort(key=lambda x: (-(x["win_rate"] or -1), x["name"]))

        # ---- 战队榜 ----
        teams_out = []
        try:
            obj = self.http.fetch_json("%s?league_id=%s" % (URL_TEAM_SETTLE, lid))
            for row in api_data(obj):
                if not isinstance(row, dict):
                    continue
                info = row.get("team_info") or {}
                st = row.get("statistics_info") or {}
                tname = (info.get("team_name") or "").strip()
                if not tname:
                    continue
                tid = str(info.get("team_id") or "") or None
                wins = as_int(st.get("victory_battle_count"))
                losses = as_int(st.get("defeated_battle_count"))
                teams_out.append(
                    {
                        "team_id": tid,
                        "name": tname,
                        "abbreviation": info.get("team_abbreviation") or None,
                        "matches": as_int(st.get("battle_count")),
                        "wins": wins,
                        "losses": losses,
                        "game_diff": (wins - losses) if (wins is not None and losses is not None) else None,
                        "win_rate": pct(st.get("win_rate")),
                        "blue_win_rate": None,   # 稍后由小局数据统计
                        "red_win_rate": None,
                        "avg_kda": num(st.get("avg_kda")),
                        "avg_kill": num(st.get("avg_kill_num")),
                        "avg_death": num(st.get("avg_death_num")),
                        "rank": None,
                    }
                )
                # 收集战队元信息（logo 只下一次）
                if tid:
                    self.teams_meta.setdefault(
                        tid,
                        {
                            "team_id": tid,
                            "name": tname,
                            "abbreviation": info.get("team_abbreviation") or None,
                            "logo_path": "/static/images/teams/%s.png" % tid,
                            "logo_ok": False,
                            "_icon_url": info.get("team_icon") or None,
                        },
                    )
        except Exception as e:
            log("    ! %s 战队榜失败：%r" % (name, e))
        teams_out.sort(key=lambda x: (-(x["win_rate"] or -1), -(x["wins"] or 0), x["name"]))
        for i, t in enumerate(teams_out, 1):
            t["rank"] = i

        return heroes_out, players_out, teams_out

    # ------------------------------------------------------------------ #
    # 4. 单个赛季：赛程 -> 小局 -> BP 明细
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 3.5 增量更新：把本地已有的对局/小局当作"已覆盖"，只补新内容
    # ------------------------------------------------------------------ #
    def _load_local_bp(self, lid):
        path = DATA_DIR / "bp_history_by_league" / ("%s.json" % lid)
        payload = read_json(path) if path.exists() else None
        if not isinstance(payload, dict):
            return None
        return payload

    def _local_coverage(self, payload):
        """返回 (已有 match_id 集合, 已有 (match_id, battle_seq) 集合, 本地最后时间)。"""
        matches = (payload or {}).get("matches") or []
        mids, keys, last = set(), set(), ""
        for m in matches:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("match_id") or "")
            if mid:
                mids.add(mid)
            keys.add((mid, as_int(m.get("battle_seq")) or 1))
            t = str(m.get("start_time") or "")
            if t > last:
                last = t
        return mids, keys, last

    def _load_local_hero_lanes(self, lid):
        """本地 hero_stats 里已有的分路数据 {ename: (lane_list, lane_games)}。"""
        path = DATA_DIR / "hero_stats_by_league" / ("%s.json" % lid)
        payload = read_json(path) if path.exists() else None
        out = {}
        for h in (payload or {}).get("heroes") or []:
            if not isinstance(h, dict):
                continue
            en = as_int(h.get("ename"))
            if en and h.get("lane_list"):
                out[en] = (list(h.get("lane_list") or []), dict(h.get("lane_games") or {}))
        return out

    def collect_league_battles(self, lg, covered_mids=None):
        """返回 (matches, blue_red_stats, lane_counts)。

        ``covered_mids``：增量模式下本地已完整抓过的 match_id 集合，这些场次直接跳过
        （省掉「赛程 → 小局列表 → 每局详情」三层请求），只抓新增场次。

        blue_red_stats: {team_name: {"blue_games":n,"blue_wins":n,"red_games":n,"red_wins":n}}
        lane_counts:    {ename: {分路: 局数}} —— v2 新增，来自 battle_player_list[].position_desc
                        （KPL 实战分路，权威；不是 herolist 的粗糙定位）
        """
        lid = lg["league_id"]
        name = lg["name"]

        # ---- 赛程 ----
        try:
            obj = self.http.fetch_json("%s?league_id=%s" % (URL_MATCHES, lid))
            schedule = [m for m in api_data(obj) if isinstance(m, dict)]
        except Exception as e:
            log("    ! %s 赛程抓取失败：%r" % (name, e))
            return [], {}, {}

        lg["match_count"] = len(schedule)

        if self.args.skip_battles:
            return [], {}, {}

        covered_mids = covered_mids or set()
        if covered_mids:
            before = len(schedule)
            schedule = [m for m in schedule if str(m.get("match_id")) not in covered_mids]
            skipped = before - len(schedule)
            if skipped:
                log("    增量：本地已有 %d 场，跳过；本次抓 %d 场" % (skipped, len(schedule)))
            if not schedule:
                log("    增量：该赛季没有新增场次")
                return [], {}, {}

        # ---- 每场赛程的小局列表 ----
        def fetch_battles(m):
            try:
                o = self.http.fetch_json("%s?match_id=%s" % (URL_BATTLES, m["match_id"]))
                return m, [b for b in api_data(o) if isinstance(b, dict)]
            except Exception:
                return m, []

        battle_jobs = []   # (match, battle_meta)
        with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            futs = [pool.submit(fetch_battles, m) for m in schedule]
            done = 0
            for fut in as_completed(futs):
                m, blist = fut.result()
                done += 1
                for b in blist:
                    if b.get("battle_id"):
                        battle_jobs.append((m, b))
                if done % 20 == 0 or done == len(schedule):
                    log_inline("    赛程 %d/%d → 已发现小局 %d" % (done, len(schedule), len(battle_jobs)))
        log("")

        if self.args.sample_battles:
            battle_jobs = battle_jobs[: self.args.sample_battles]

        # ---- 小局详情（含完整 bp_list）----
        def fetch_detail(job):
            m, b = job
            try:
                o = self.http.fetch_json("%s?battle_id=%s" % (URL_BATTLE_DETAIL, b["battle_id"]))
                d = o.get("data") if isinstance(o, dict) else None
                return (m, b, d if isinstance(d, dict) else None)
            except Exception:
                return (m, b, None)

        details = []
        total = len(battle_jobs)
        with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            futs = [pool.submit(fetch_detail, j) for j in battle_jobs]
            done = 0
            for fut in as_completed(futs):
                details.append(fut.result())
                done += 1
                if done % 10 == 0 or done == total:
                    log_inline("    BP 明细 %d/%d（%.0f%%）" % (done, total, 100.0 * done / max(total, 1)))
        log("")

        # ---- 组装 ----
        matches_out = []
        br = defaultdict(lambda: {"blue_games": 0, "blue_wins": 0, "red_games": 0, "red_wins": 0})
        # v2：该赛季每个英雄的实战分路局数 {ename: {分路: 局数}}
        lane_counts = defaultdict(lambda: defaultdict(int))
        seen = set()

        for m, b, d in details:
            if not d:
                continue
            mid = str(m.get("match_id"))
            seq_no = as_int(b.get("battle_seq")) or as_int(d.get("battle_seq")) or 1
            dedup = (mid, seq_no)
            if dedup in seen:
                continue
            seen.add(dedup)

            camp1 = d.get("camp1") or {}
            camp2 = d.get("camp2") or {}
            blue_team = camp1.get("team_name") or None
            red_team = camp2.get("team_name") or None
            win_camp = as_int(d.get("win_camp"))
            winner = "blue" if win_camp == 1 else ("red" if win_camp == 2 else None)

            # 蓝红方胜率统计（camp1 = 蓝方，实测确认：bp_list 第 1 手为 camp1 ban）
            if blue_team:
                br[blue_team]["blue_games"] += 1
                if winner == "blue":
                    br[blue_team]["blue_wins"] += 1
            if red_team:
                br[red_team]["red_games"] += 1
                if winner == "red":
                    br[red_team]["red_wins"] += 1

            # v2：统计本局 10 名上场英雄的真实分路（KPL 实战 position_desc，权威数据）
            for p in d.get("battle_player_list") or []:
                if not isinstance(p, dict):
                    continue
                en = as_int(p.get("hero_id"))
                lane_name = (p.get("position_desc") or "").strip()
                if not en or lane_name not in VALID_LANES:
                    continue
                lane_counts[en][lane_name] += 1

            sequence = []
            blue_picks, red_picks, blue_bans, red_bans = [], [], [], []
            for idx, item in enumerate(d.get("bp_list") or []):
                if not isinstance(item, dict):
                    continue
                camp = as_int(item.get("camp"))
                side = "blue" if camp == 1 else ("red" if camp == 2 else None)
                if side is None:
                    continue
                action = "ban" if as_int(item.get("is_ban_or_pick")) == 0 else "pick"
                hero = item.get("hero_name") or None
                ename = as_int(item.get("hero_id"))
                if not hero:
                    continue
                sequence.append(
                    {
                        "step": idx + 1,
                        "side": side,
                        "action": action,
                        "hero": hero,
                        "ename": ename,
                    }
                )
                if action == "pick":
                    (blue_picks if side == "blue" else red_picks).append(hero)
                else:
                    (blue_bans if side == "blue" else red_bans).append(hero)

            if not sequence:
                continue

            matches_out.append(
                {
                    "match_id": mid,
                    "battle_id": str(b.get("battle_id") or d.get("battle_id") or ""),
                    "battle_seq": seq_no,
                    "bo": as_int(m.get("bo")),
                    "stage": m.get("match_stage_desc") or None,
                    "start_time": m.get("start_time") or None,
                    "blue_team": blue_team,
                    "red_team": red_team,
                    "winner": winner,
                    "sequence": sequence,
                    "blue_picks": blue_picks,
                    "red_picks": red_picks,
                    "blue_bans": blue_bans,
                    "red_bans": red_bans,
                }
            )

        matches_out.sort(key=lambda x: (x.get("start_time") or "", x["match_id"], x["battle_seq"]))
        return matches_out, dict(br), {en: dict(cnt) for en, cnt in lane_counts.items()}

    # ------------------------------------------------------------------ #
    # 4.5 实战分路：写进 hero_stats（按赛季）与 heroes.json（全量）
    # ------------------------------------------------------------------ #
    @staticmethod
    def apply_lane_stats(heroes, lane_counts):
        """把 {ename: {分路: 局数}} 回填进 hero_stats 的英雄列表。

        - ``lane_list``：该赛季真实分路（频次降序 + Lanes 固定序）
        - ``lane_games``：{分路: 局数}
        - 该赛季没有该英雄分路数据 → ``lane_list=[]``、``lane_games={}``，
          **绝不回退到 herolist 的 hero_type/role**（用户明确要求）
        """
        lane_counts = lane_counts or {}
        for h in heroes:
            if not isinstance(h, dict):
                continue
            en = as_int(h.get("ename"))
            counts = lane_counts.get(en) or {}
            # 只保留合法分路
            counts = {k: int(v) for k, v in counts.items() if k in VALID_LANES and v}
            h["lane_list"] = lane_sort_key(counts)
            h["lane_games"] = counts
            h["lane_source"] = "kpl_battle_position"
        return heroes

    def finalize_hero_lanes(self):
        """汇总全部赛季的实战分路，写回 heroes.json 的 lane/lane_list/lane_leagues。

        ``lane_list``（全量）只是索引与兜底展示；**对局与筛选必须用赛季数据**。
        """
        self._hero_stats_written = getattr(self, "_hero_stats_written", set())
        if not self._league_lane_counts:
            log("  ! 没有任何赛季的实战分路数据，heroes.json 的 lane_list 全部为空数组")
        totals = defaultdict(lambda: defaultdict(int))
        leagues_seen = defaultdict(set)
        for lid, counts in (self._league_lane_counts or {}).items():
            for en, cnt in counts.items():
                for lane_name, games in cnt.items():
                    if lane_name not in VALID_LANES or not games:
                        continue
                    totals[en][lane_name] += int(games)
                    leagues_seen[en].add(lid)

        filled = 0
        official = 0
        for h in self.heroes:
            if not isinstance(h, dict):
                continue
            en = as_int(h.get("ename"))
            cnt = dict(totals.get(en) or {})
            lane_list = lane_sort_key(cnt)
            if lane_list:
                # 有 KPL 实战分路：以实战为准（最权威）
                h["lane_list"] = lane_list
                h["lane"] = lane_list[0]
                h["lane_source"] = "kpl_battle_position"
                h["lane_leagues"] = len(leagues_seen.get(en) or ())
                filled += 1
            else:
                # v43：KPL 没出场过 → 用官方 herolist.json 的分路 code（roles + extra_cold_lane），
                # 不再退回 hero_type 的粗糙猜测。
                off = [ln for ln in (h.get("lane_official") or []) if ln in VALID_LANES]
                h["lane_list"] = off
                h["lane"] = off[0] if off else None
                h["lane_source"] = "pvp_qq_official" if off else "missing"
                h["lane_leagues"] = 0
                if off:
                    official += 1

        write_json(DATA_DIR / "heroes.json", {"heroes": self.heroes, "_fetched_at": TODAY})
        log(
            "  ✓ heroes.json 分路：%d 个来自 KPL 实战、%d 个回退官方分路、%d 个仍缺失（共 %d）"
            % (filled, official, len(self.heroes) - filled - official, len(self.heroes))
        )

    # ------------------------------------------------------------------ #
    # 4.5 英雄关系（官方详情页：压制英雄 / 被压制英雄）—— v43
    # ------------------------------------------------------------------ #
    def step_hero_relations(self):
        """抓官方英雄详情页的「英雄关系」，产出 data/hero_relations.json。

        数据源：`https://pvp.qq.com/web201605/herodetail/{ename}.shtml`（**GBK 编码**）。
        页面「英雄关系」分三块：最佳搭档 / 压制英雄 / 被压制英雄，每块最多 2 个英雄，
        都是 `<a href="{ename}.shtml">` 链接，直接取 href 里的 ename 最稳。
        抓不到的（新英雄还没详情页，例如元流之子 581~585 返回 404）一律写 None，绝不编造。
        """
        by_ename = {as_int(h.get("ename")): h for h in self.heroes if isinstance(h, dict)}
        rows = []
        missing = []
        for index, h in enumerate(self.heroes, 1):
            if not isinstance(h, dict):
                continue
            en = as_int(h.get("ename"))
            if not en:
                continue
            html = None
            used_url = None
            id_name = str(h.get("id_name") or "").strip()
            candidates = [(URL_HERO_DETAIL.format(ename=en), "ename")]
            if id_name:
                candidates.append((URL_HERO_DETAIL_BY_NAME.format(id_name=id_name), "id_name"))
            last_exc = None
            for url, how in candidates:
                try:
                    html = self.http.fetch_bytes(url).decode("gbk", "replace")
                    used_url = url
                    if how == "id_name":
                        log("    · 英雄关系 %s(%d) 用拼音页命中：%s" % (h.get("cname"), en, id_name))
                    break
                except Exception as exc:                    # noqa: BLE001
                    last_exc = exc
                    html = None
            if html is None:
                missing.append(en)
                log("    · 英雄关系 %s(%d) 抓取失败：%r" % (h.get("cname"), en, last_exc))

            counters = countered = partners = None
            if html:
                try:
                    blocks = self._split_hero_relations(html)
                    counters = self._relation_enames(blocks.get("压制英雄"))
                    countered = self._relation_enames(blocks.get("被压制英雄"))
                    partners = self._relation_enames(blocks.get("最佳搭档"))
                except Exception as exc:                    # noqa: BLE001
                    log("    · 英雄关系 %s(%d) 解析失败：%r" % (h.get("cname"), en, exc))
                    counters = countered = partners = None

            def _names(items):
                if items is None:
                    return None
                out = []
                for one in items:
                    name = (by_ename.get(one) or {}).get("cname")
                    if name and name not in out:
                        out.append(name)
                return out

            rows.append({
                "ename": en,
                "cname": h.get("cname"),
                "counters": _names(counters),
                "counters_enames": counters,
                "countered_by": _names(countered),
                "countered_by_enames": countered,
                "partners": _names(partners),
                "partners_enames": partners,
                "source_url": used_url,
            })
            if index % 20 == 0:
                log("    英雄关系进度 %d/%d" % (index, len(self.heroes)))

        write_json(
            DATA_DIR / "hero_relations.json",
            {
                "heroes": rows,
                "_source": "pvp.qq.com/web201605/herodetail",
                "_fetched_at": TODAY,
                "_missing": missing,
                "_missing_count": len(missing),
            },
        )
        have = sum(1 for r in rows if r.get("counters") is not None)
        log("  ✓ 英雄关系：%d/%d 个英雄有官方克制数据，%d 个抓不到（记 null）"
            % (have, len(rows), len(missing)))

    @staticmethod
    def _split_hero_relations(html):
        """把详情页「英雄关系」区块按 最佳搭档 / 压制英雄 / 被压制英雄 切开。"""
        out = {}
        markers = ("最佳搭档", "压制英雄", "被压制英雄")
        positions = []
        for name in markers:
            found = -1
            cursor = 0
            while True:
                pos = html.find(name, cursor)
                if pos < 0:
                    break
                if "hero-f1" in html[max(0, pos - 200):pos]:
                    found = pos
                    break
                cursor = pos + len(name)
            positions.append((found if found >= 0 else len(html), name))
        positions.sort()
        for i, (pos, name) in enumerate(positions):
            end = positions[i + 1][0] if i + 1 < len(positions) else len(html)
            out[name] = html[pos:end]
        return out

    @staticmethod
    def _relation_enames(block):
        """从关系区块里取 ename；没有该区块（或没有匹配）返回 None / []。"""
        if not block:
            return None
        hits = re_mod.findall(r'href="(\d+)\.shtml"', block)
        out = []
        for one in hits:
            val = as_int(one)
            if val and val not in out:
                out.append(val)
        return out

    # ------------------------------------------------------------------ #
    # 5. 战队 logo
    # ------------------------------------------------------------------ #
    def step_team_logos(self):
        log("【5/5】下载战队 logo …")
        teams = list(self.teams_meta.values())
        if not teams:
            log("  没有战队元信息，跳过")
            write_json(DATA_DIR / "team_meta.json", {"teams": [], "_fetched_at": TODAY})
            return

        def work(t):
            url = t.get("_icon_url")
            if not url:
                return t, False
            dest = STATIC_IMG_TEAMS / ("%s.png" % t["team_id"])
            return t, self.http.download_file(url, dest)

        ok = 0
        with ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            for fut in as_completed([pool.submit(work, t) for t in teams]):
                try:
                    t, good = fut.result()
                except Exception:
                    continue
                t["logo_ok"] = bool(good)
                if good:
                    ok += 1
        out = []
        for t in teams:
            t.pop("_icon_url", None)
            out.append(t)
        out.sort(key=lambda x: x["name"])
        write_json(DATA_DIR / "team_meta.json", {"teams": out, "_fetched_at": TODAY})
        log("  ✓ 写入 data/team_meta.json（%d 支战队，%d 个 logo）" % (len(out), ok))

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def run(self):
        t0 = time.time()
        log("=" * 68)
        log("KPL 模拟 BP 小游戏 · 真实数据采集")
        log("数据源：腾讯王者荣耀官方联赛接口（prod.comp.smoba.qq.com / pvp.qq.com）")
        log("=" * 68)

        self.ensure_dirs()
        self.step_heroes()
        self.step_leagues()

        if not self.leagues:
            log("✗ 没有枚举到任何赛事，退出。")
            return 1

        n = len(self.leagues)
        for i, lg in enumerate(self.leagues, 1):
            lid = lg["league_id"]
            log("")
            log("【榜单 %d/%d】%s  %s" % (i, n, lid, lg["name"]))
            heroes, players, teams = self.collect_league_boards(lg)
            log("    英雄 %d · 选手 %d · 战队 %d" % (len(heroes), len(players), len(teams)))

            # v2：hero_stats 先不落盘 —— 等 BP 阶段统计出实战分路后，
            # 用 apply_lane_stats() 回填 lane_list/lane_games 再一次性写入。
            self._pending_hero_stats = getattr(self, "_pending_hero_stats", {})
            self._pending_hero_stats[lid] = (lg, heroes)
            write_json(
                DATA_DIR / "player_stats_by_league" / ("%s.json" % lid),
                {
                    "league_id": lid,
                    "league_name": lg["name"],
                    "players": players,
                    "_fetched_at": TODAY,
                },
            )
            # 战队榜先落盘（蓝红胜率稍后补）
            self._pending_team_stats = getattr(self, "_pending_team_stats", {})
            self._pending_team_stats[lid] = (lg, teams)

        # ---- BP 历史 + 蓝红胜率 + 实战分路 ----
        bp_summary = []
        # v2：以下三个容器在【榜单】阶段就已建立，这里不能重置（否则会丢掉待回填的 hero_stats）
        self._league_lane_counts = getattr(self, "_league_lane_counts", {})
        self._pending_hero_stats = getattr(self, "_pending_hero_stats", {})
        self._hero_stats_written = getattr(self, "_hero_stats_written", set())
        for i, lg in enumerate(self.leagues, 1):
            lid = lg["league_id"]
            log("")
            log("【BP %d/%d】%s  %s" % (i, n, lid, lg["name"]))
            local_payload = self._load_local_bp(lid) if getattr(self.args, "incremental", False) else None
            local_mids, local_keys, local_last = self._local_coverage(local_payload)
            if local_payload:
                log("    本地已有 %d 局%s" % (
                    len(local_payload.get("matches") or []),
                    ("（最后一场 %s）" % local_last) if local_last else ""))
            matches, br, lane_counts = self.collect_league_battles(lg, covered_mids=local_mids)

            # 增量：把本地已有对局合并回来（新的优先，按时间排序）
            if local_payload:
                merged = {}
                for m in (local_payload.get("matches") or []):
                    if isinstance(m, dict):
                        merged[(str(m.get("match_id")), as_int(m.get("battle_seq")) or 1)] = m
                for m in matches:
                    merged[(str(m.get("match_id")), as_int(m.get("battle_seq")) or 1)] = m
                matches = sorted(
                    merged.values(),
                    key=lambda x: (x.get("start_time") or "", str(x.get("match_id")), x.get("battle_seq") or 0),
                )
                log("    合并后共 %d 局（新增 %d 局）" % (len(matches), len(matches) - len(local_payload.get("matches") or [])))

            # v2：把该赛季的实战分路写进 hero_stats（权威字段，跟着赛季走）
            # 增量模式下本次只抓新增对局，分路局数必须与本地已有的合并，
            # 否则会把老赛季的分路统计冲成空（heroes.json 的汇总也会跟着丢）。
            merged_counts = {}
            if local_payload:
                for _en, (_ll, _lg) in (self._load_local_hero_lanes(lid) or {}).items():
                    merged_counts[_en] = dict(_lg)
            for _en, _cnt in (lane_counts or {}).items():
                bucket = merged_counts.setdefault(_en, {})
                for _k, _v in (_cnt or {}).items():
                    bucket[_k] = int(bucket.get(_k, 0)) + int(_v)
            self._league_lane_counts[lid] = merged_counts
            pend_h = getattr(self, "_pending_hero_stats", {})
            if lid in pend_h:
                lg_h, heroes_h = pend_h[lid]
                self.apply_lane_stats(heroes_h, merged_counts)
                if local_payload:
                    log("    分路统计：本地 %d 个英雄 + 新增 %d 个英雄 → 合并 %d 个"
                        % (len(self._load_local_hero_lanes(lid) or {}), len(lane_counts or {}),
                           len(merged_counts)))
                write_json(
                    DATA_DIR / "hero_stats_by_league" / ("%s.json" % lid),
                    {
                        "league_id": lid,
                        "league_name": lg_h["name"],
                        "heroes": heroes_h,
                        "_lane_source": "kpl_battle_position:position_desc",
                        "_lane_missing": not any(
                            h.get("lane_list") for h in heroes_h
                        ),
                        "_fetched_at": TODAY,
                    },
                )
                self._hero_stats_written.add(lid)
                lane_n = sum(1 for h in heroes_h if h.get("lane_list"))
                log("    实战分路：%d/%d 个英雄有分路数据" % (lane_n, len(heroes_h)))

            step_counts = [len(m["sequence"]) for m in matches]
            payload = {
                "league_id": lid,
                "league_name": lg["name"],
                "matches": matches,
                "_step_count": mode_of(step_counts),
                "_missing": len(matches) == 0,
                "_fetched_at": TODAY,
            }
            write_json(DATA_DIR / "bp_history_by_league" / ("%s.json" % lid), payload)
            bp_summary.append((lid, lg["name"], len(matches), mode_of(step_counts) or 0))
            log("    ✓ %d 局真实 BP（每局 %s 手）" % (len(matches), mode_of(step_counts)))

            # 回填蓝红方胜率
            pend = getattr(self, "_pending_team_stats", {})
            if lid in pend and br:
                lg2, teams = pend[lid]
                for t in teams:
                    s = br.get(t["name"])
                    if not s:
                        continue
                    if s["blue_games"] > 0:
                        t["blue_win_rate"] = round(100.0 * s["blue_wins"] / s["blue_games"], 2)
                    if s["red_games"] > 0:
                        t["red_win_rate"] = round(100.0 * s["red_wins"] / s["red_games"], 2)
                write_json(
                    DATA_DIR / "team_stats_by_league" / ("%s.json" % lid),
                    {
                        "league_id": lid,
                        "league_name": lg2["name"],
                        "teams": teams,
                        "_camp_mapping": "camp1=blue",
                        "_fetched_at": TODAY,
                    },
                )

        # ---- 没有 BP 数据的赛季也要落战队榜 ----
        for lid, (lg2, teams) in getattr(self, "_pending_team_stats", {}).items():
            path = DATA_DIR / "team_stats_by_league" / ("%s.json" % lid)
            if not path.exists():
                write_json(
                    path,
                    {
                        "league_id": lid,
                        "league_name": lg2["name"],
                        "teams": teams,
                        "_camp_mapping": "camp1=blue",
                        "_fetched_at": TODAY,
                    },
                )

        # ---- v2：没有 BP 数据（或跳过 BP）的赛季，hero_stats 也要落盘 ----
        for lid, (lg_h, heroes_h) in getattr(self, "_pending_hero_stats", {}).items():
            if lid in self._hero_stats_written:
                continue
            self.apply_lane_stats(heroes_h, {})
            write_json(
                DATA_DIR / "hero_stats_by_league" / ("%s.json" % lid),
                {
                    "league_id": lid,
                    "league_name": lg_h["name"],
                    "heroes": heroes_h,
                    "_lane_source": "kpl_battle_position:position_desc",
                    "_lane_missing": True,
                    "_fetched_at": TODAY,
                },
            )

        # ---- v2：把全部赛季的实战分路汇总进 heroes.json ----
        # v43：KPL 没出场过的英雄，分路回退到官方 herolist.json 的分路 code
        self.finalize_hero_lanes()

        # ---- v43：英雄关系（官方详情页 压制/被压制）----
        self.step_hero_relations()

        self.step_team_logos()

        # ---- 赛事索引 ----
        write_json(
            DATA_DIR / "leagues.json",
            {
                "leagues": self.leagues,
                "default_league_id": self.default_league_id(),
                "_fetched_at": TODAY,
            },
        )

        elapsed = time.time() - t0
        log("")
        log("=" * 68)
        log("✔ 采集完成，用时 %.1f 秒" % elapsed)
        log("  HTTP: %s" % dict(self.http.stats))
        log("-" * 68)
        log("  %-10s %-22s %8s %8s" % ("league_id", "赛事", "BP局数", "每局手数"))
        for lid, nm, cnt, sc in bp_summary:
            log("  %-10s %-22s %8d %8s" % (lid, nm, cnt, sc))
        log("=" * 68)
        return 0


# --------------------------------------------------------------------------- #
# 数据完整性自检（用户要求的 交付前自检 #1）
# --------------------------------------------------------------------------- #
def refresh_hero_extra(progress=None) -> int:
    """只刷新「王者荣耀官网英雄数据」：官方分路（roles / extra_cold_lane）+ 英雄关系。

    与 KPL 数据解耦：不会重抓赛事/对局，也不会覆盖已有的 KPL 实战分路
    （heroes.json 里 lane/lane_list/lane_source=kpl_battle_position 的记录原样保留）。
    """
    import argparse as _argparse

    args = _argparse.Namespace(
        force=False, skip_battles=True, sample_battles=0, workers=3,
        incremental=True, full=False, verify=False,
    )
    s = Scraper(args)
    s.ensure_dirs()

    # 0) 先读旧 heroes.json（``step_heroes()`` 会把它整份覆盖成占位值，必须先读）
    old_path = DATA_DIR / "heroes.json"
    old_rows = []
    try:
        payload = json.loads(old_path.read_text(encoding="utf-8"))
        old_rows = payload.get("heroes") or []
    except Exception:  # noqa: BLE001
        old_rows = []
    old_by = {as_int(r.get("ename")): r for r in old_rows if isinstance(r, dict)}

    # 1) 重新读官方 hero list，刷新 role / lane_official（保留已有 KPL 分路）
    log("=" * 68)
    log("刷新「王者荣耀官网」英雄数据：官方分路 + 英雄关系")
    log("=" * 68)
    s.step_heroes()

    merged = 0
    for h in s.heroes:
        en = as_int(h.get("ename"))
        prev = old_by.get(en) or {}
        # 官网字段全部用新抓的覆盖：role / lane_official
        # KPL 实战分路（lane / lane_list / lane_source / lane_leagues）沿用旧值
        prev_lanes = [x for x in (prev.get("lane_list") or []) if x in VALID_LANES]
        if prev.get("lane_source") == "kpl_battle_position" and prev_lanes:
            # 真有 KPL 实战分路才保留（lane_list 为空的说明其实没数据，要走官网兜底）
            for key in ("lane", "lane_list", "lane_source", "lane_leagues", "lane_games"):
                if key in prev:
                    h[key] = prev[key]
        else:
            off = [ln for ln in (h.get("lane_official") or []) if ln in VALID_LANES]
            h["lane"] = off[0] if off else None
            h["lane_list"] = off
            h["lane_source"] = "pvp_qq_official" if off else "missing"
            h["lane_leagues"] = 0
        for key in ("avatar_path", "avatar_ok"):
            if key in prev:
                h[key] = prev[key]
        merged += 1

    write_json(DATA_DIR / "heroes.json", {"heroes": s.heroes, "_fetched_at": TODAY})
    log("  ✓ heroes.json 已更新官方分路：%d 个英雄（KPL 实战分路原样保留）" % merged)
    if progress:
        progress("官方分路已更新，开始抓英雄关系…")

    # 2) 英雄关系
    s.step_hero_relations()
    if progress:
        progress("英雄关系已更新")
    return 0


def verify_data() -> int:
    log("=" * 68)
    log("数据完整性自检")
    log("=" * 68)
    problems = []
    warns = []

    def p(path):
        return DATA_DIR / path

    # 1) heroes.json
    hj = read_json(p("heroes.json"))
    heroes = (hj or {}).get("heroes") or []
    if len(heroes) >= 100:
        log("  ✓ heroes.json：%d 个英雄（要求 ≥100）" % len(heroes))
    else:
        problems.append("heroes.json 只有 %d 个英雄（要求 ≥100）" % len(heroes))

    # 2) 头像
    imgs = list(STATIC_IMG_HEROES.glob("*.jpg")) if STATIC_IMG_HEROES.exists() else []
    imgs = [f for f in imgs if f.stat().st_size > 512]
    if len(imgs) >= 100:
        log("  ✓ static/images/heroes/：%d 张真实头像（要求 ≥100）" % len(imgs))
    else:
        problems.append("头像只有 %d 张（要求 ≥100）" % len(imgs))

    # 3) leagues.json
    lj = read_json(p("leagues.json")) or {}
    leagues = lj.get("leagues") or []
    if len(leagues) >= 6:
        log("  ✓ leagues.json：%d 个赛事（要求 ≥6，目标 8）%s"
            % (len(leagues), "" if len(leagues) >= 8 else "  [低于目标]"))
        if len(leagues) < 8:
            warns.append("赛事数 %d < 目标 8" % len(leagues))
    else:
        problems.append("leagues.json 只有 %d 个赛事（要求 ≥6）" % len(leagues))

    default_lid = lj.get("default_league_id")
    ended = [x for x in leagues if x.get("status") == "ended"]
    if ended and default_lid != ended[-1]["league_id"]:
        warns.append("default_league_id=%r 不是最后一个已结束赛事 %r"
                     % (default_lid, ended[-1]["league_id"]))
    else:
        log("  ✓ default_league_id = %s" % default_lid)

    # 4) 逐赛季检查
    log("-" * 68)
    log("  %-10s %-20s %7s %7s %7s %8s %s"
        % ("league_id", "赛事", "英雄", "选手", "战队", "BP局数", "状态"))
    for lg in leagues:
        lid = lg["league_id"]
        hs = read_json(DATA_DIR / "hero_stats_by_league" / ("%s.json" % lid))
        ps = read_json(DATA_DIR / "player_stats_by_league" / ("%s.json" % lid))
        ts = read_json(DATA_DIR / "team_stats_by_league" / ("%s.json" % lid))
        bs = read_json(DATA_DIR / "bp_history_by_league" / ("%s.json" % lid))

        nh = len((hs or {}).get("heroes") or [])
        np_ = len((ps or {}).get("players") or [])
        nt = len((ts or {}).get("teams") or [])
        bm = (bs or {}).get("matches") or []
        missing = (bs or {}).get("_missing")

        flags = []
        if nh < 20:
            problems.append("%s 英雄数据只有 %d 条（应为几十个）" % (lid, nh))
            flags.append("英雄少")
        if nt < 2:
            problems.append("%s 战队数据只有 %d 条" % (lid, nt))
            flags.append("战队少")
        if bs is None:
            problems.append("%s 缺少 bp_history 文件" % lid)
            flags.append("缺BP文件")
        elif not bm:
            if not missing:
                problems.append("%s bp_history 为空但没有标 _missing" % lid)
            flags.append("BP空")
        if np_ == 0:
            warns.append("%s 选手数据为空" % lid)

        log("  %-10s %-20s %7d %7d %7d %8d %s"
            % (lid, lg["name"][:18], nh, np_, nt, len(bm), ",".join(flags) or "OK"))

    # 5) 战队 logo
    logos = list(STATIC_IMG_TEAMS.glob("*.png")) if STATIC_IMG_TEAMS.exists() else []
    logos = [f for f in logos if f.stat().st_size > 512]
    if logos:
        log("  ✓ static/images/teams/：%d 个战队 logo" % len(logos))
    else:
        warns.append("没有战队 logo")

    # 6) 蓝红胜率覆盖
    for lg in leagues:
        ts = read_json(DATA_DIR / "team_stats_by_league" / ("%s.json" % lg["league_id"])) or {}
        teams = ts.get("teams") or []
        withblue = [t for t in teams if t.get("blue_win_rate") is not None]
        if teams and not withblue:
            warns.append("%s 没有蓝方胜率（可能无小局数据）" % lg["league_id"])

    # 7) 抽查 BP 序列结构
    checked = 0
    for lg in leagues:
        bs = read_json(DATA_DIR / "bp_history_by_league" / ("%s.json" % lg["league_id"])) or {}
        ms = bs.get("matches") or []
        if not ms:
            continue
        m = ms[0]
        seq = m.get("sequence") or []
        ok_keys = all(
            set(x.keys()) >= {"step", "side", "action", "hero", "ename"} for x in seq
        )
        ok_side = all(x["side"] in ("blue", "red") for x in seq)
        ok_act = all(x["action"] in ("ban", "pick") for x in seq)
        if not (ok_keys and ok_side and ok_act):
            problems.append("%s BP 序列结构异常" % lg["league_id"])
        else:
            checked += 1
    if checked:
        log("  ✓ 有 %d 个赛季的 BP 序列结构校验通过" % checked)

    # 8) v2：英雄分路（KPL 实战分路）覆盖情况
    log("-" * 68)
    log("  v2 英雄分路（来源：KPL 实战 position_desc，禁止用 herolist 定位）")
    heroes_no_lane = 0
    official_fallback = []
    for h in heroes:
        ll = h.get("lane_list")
        if not isinstance(ll, list):
            problems.append("heroes.json 的 %s(%s) 缺 lane_list 字段" % (h.get("cname"), h.get("ename")))
            continue
        if not ll:
            heroes_no_lane += 1
            continue
        bad = [x for x in ll if x not in VALID_LANES]
        if bad:
            problems.append("heroes.json 的 %s 分路含非法值 %r" % (h.get("cname"), bad))
        src = h.get("lane_source")
        if src not in ("kpl_battle_position", "pvp_qq_official"):
            problems.append("heroes.json 的 %s lane_source 非法：%r" % (h.get("cname"), src))
        elif src == "pvp_qq_official":
            # v43：KPL 没出场过 → 分路回退到王者荣耀官网 herolist 的分路 code，属于正常情况
            official_fallback.append(str(h.get("cname")))
    log("  heroes.json：%d/%d 个英雄有实战分路（%d 个无数据，符合源数据缺失）"
        % (len(heroes) - heroes_no_lane, len(heroes), heroes_no_lane))
    if official_fallback:
        log("  ✓ 其中 %d 个 KPL 未出场英雄回退到王者荣耀官网官方分路：%s"
            % (len(official_fallback), "、".join(official_fallback[:8])
               + ("…" if len(official_fallback) > 8 else "")))

    for lg in leagues:
        lid = lg["league_id"]
        hs = read_json(DATA_DIR / "hero_stats_by_league" / ("%s.json" % lid)) or {}
        rows = hs.get("heroes") or []
        with_lane = [h for h in rows if isinstance(h.get("lane_list"), list) and h["lane_list"]]
        if "_lane_missing" not in hs:
            problems.append("%s 的 hero_stats 缺 _lane_missing 标记" % lid)
        if rows and not with_lane and not hs.get("_lane_missing"):
            problems.append("%s 无任何分路数据但没标 _lane_missing" % lid)
        log("  %-10s hero_stats 分路覆盖 %d/%d%s"
            % (lid, len(with_lane), len(rows), "  [整季缺分路]" if hs.get("_lane_missing") else ""))

    # 9) v2：热门英雄分路抽验（用户点名的校验标准）
    lane_index = {h.get("cname"): (h.get("lane_list") or []) for h in heroes if isinstance(h, dict)}
    spot = {
        "关羽": "对抗路",
        "澜": "打野",
        "孙膑": "游走",
        "马超": "对抗路",
    }
    log("-" * 68)
    log("  热门英雄分路抽验（用户明确要求：关羽不能只标坦克、澜必须打野、孙膑游走）")
    for cname, expect in spot.items():
        if cname not in lane_index:
            warns.append("抽验英雄 %s 不在英雄总表里" % cname)
            continue
        got = lane_index[cname]
        ok = expect in got
        log("  %s %s %s → %s" % ("✓" if ok else "✗", cname, expect, got or "[]"))
        if not ok:
            problems.append(
                "%s 的分路应为 %s 之一，实际 %r（请检查 position_desc 采集）"
                % (cname, expect, got)
            )
    log("  马超多分路说明：实测 对抗路+打野，属源数据真实情况（一个英雄多标签）")

    # 结论
    log("-" * 68)
    if problems:
        log("✗ 发现 %d 个问题：" % len(problems))
        for x in problems:
            log("   - " + x)
    if warns:
        log("! %d 个提醒：" % len(warns))
        for x in warns:
            log("   - " + x)
    if not problems:
        log("✔ 自检全部通过（%d 个提醒）" % len(warns))
    log("=" * 68)
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="抓取真实 KPL 赛事数据（英雄/选手/战队/BP 历史）"
    )
    parser.add_argument("--force", action="store_true", help="忽略本地缓存，全量重抓")
    parser.add_argument("--verify", action="store_true", help="只做数据完整性自检，不联网")
    parser.add_argument("--skip-battles", action="store_true",
                        help="快速模式：不抓小局 BP 明细（只抓榜单）")
    parser.add_argument("--sample-battles", type=int, default=0,
                        help="每赛季最多抓 N 个小局（调试用，0=全量）")
    parser.add_argument("--workers", type=int, default=5, help="并发数（默认 5）")
    parser.add_argument("--incremental", action="store_true",
                        help="增量抓取：本地已有对局直接跳过，只补新赛季/新场次（游戏内更新默认用这个）")
    parser.add_argument("--full", action="store_true",
                        help="全量重抓（等价于 --force，忽略增量）")
    args = parser.parse_args(argv)

    # Windows 控制台中文
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.verify:
        return verify_data()

    if args.full:
        args.force = True
    if not hasattr(args, "incremental"):
        args.incremental = False

    try:
        rc = Scraper(args).run()
    except KeyboardInterrupt:
        log("\n! 用户中断。已抓到的进度都在 .scrape_cache/，重跑会续抓。")
        return 130
    except Exception:
        log("\n✗ 采集过程出现未预期错误：")
        traceback.print_exc()
        return 1

    log("")
    verify_data()
    return rc


if __name__ == "__main__":
    sys.exit(main())
