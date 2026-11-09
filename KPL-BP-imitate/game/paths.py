"""统一路径解析（源码运行 / PyInstaller 打包 两种形态都适用）。

v10 打包说明
------------
* 源码运行时：``resource_dir()`` == ``data_dir()`` == 项目根目录 ``kpl_bp_game/``，
  行为与以前完全一致，不改变任何既有路径。
* PyInstaller 打包后：

  - ``resource_dir()``：只读资源目录（``sys._MEIPASS``，onefile 时是临时解压目录），
    里面带着 templates / static / data 的**初始副本**。
  - ``data_dir()``：**可写**数据目录，固定放在 exe 同级的 ``data/``，
    这样存档、配置不会因为 exe 退出（onefile 会清空临时目录）而丢失。

首次启动时会把内置的 ``data/`` 复制到 exe 旁边（只复制缺失的文件），
所以玩家拿到 exe 双击后，数据、头像、存档都在 exe 旁边的 data 文件夹里。
"""

import os
import shutil
import sys
from typing import Optional

#: 判断是否需要「把内置数据种到可写目录」的标记文件。
_SEED_MARKERS = ("heroes.json", "leagues.json")


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的可执行文件里。"""
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> str:
    """exe 所在目录（源码运行时等于项目根目录）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return resource_dir()


def resource_dir() -> str:
    """只读资源根目录（templates / static / data 的初始副本）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return meipass
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _seed_dir() -> str:
    return os.path.join(resource_dir(), "data")


def _copy_missing(src: str, dst: str) -> int:
    """把 src 下缺失的文件补到 dst（不覆盖已有文件）。返回复制数量。"""
    copied = 0
    for cur, _dirs, files in os.walk(src):
        rel = os.path.relpath(cur, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            continue
        for name in files:
            s = os.path.join(cur, name)
            d = os.path.join(target, name)
            if os.path.exists(d):
                continue
            try:
                shutil.copy2(s, d)
                copied += 1
            except OSError as exc:  # 只打日志，绝不让启动失败
                print("[paths] 复制数据失败 %s -> %s：%s" % (s, d, exc))
    return copied


def ensure_seed_data(quiet: bool = False) -> None:
    """打包运行时，把内置 data/ 种到 exe 旁边（幂等、只补缺失）。"""
    if not is_frozen():
        return
    src = _seed_dir()
    dst = os.path.join(exe_dir(), "data")
    if os.path.abspath(src) == os.path.abspath(dst) or not os.path.isdir(src):
        return
    if all(os.path.exists(os.path.join(dst, m)) for m in _SEED_MARKERS):
        return
    started = os.path.exists(dst)
    copied = _copy_missing(src, dst)
    if not quiet:
        print("[paths] 首次启动：已把内置数据（%d 个文件）准备到 %s%s"
              % (copied, dst, "" if started else "（新建）"))


_DATA_DIR_CACHE: Optional[str] = None


def data_dir() -> str:
    """可写数据目录：打包后为 ``<exe 同级>/data``，源码运行为 ``<项目>/data``。"""
    global _DATA_DIR_CACHE
    if _DATA_DIR_CACHE:
        return _DATA_DIR_CACHE
    if is_frozen():
        ensure_seed_data()
        path = os.path.join(exe_dir(), "data")
    else:
        path = _seed_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        print("[paths] 无法创建数据目录 %s：%s" % (path, exc))
    _DATA_DIR_CACHE = path
    return path


def data_path(*parts: str) -> str:
    """拼一个数据文件路径。"""
    return os.path.join(data_dir(), *parts)


# --------------------------------------------------------------------------- #
# v33：图片资源也外置（官方会出新英雄 / 新战队，不该锁在 exe 里）
# --------------------------------------------------------------------------- #
_IMAGES_DIR_CACHE: Optional[str] = None


def _seed_images() -> str:
    return os.path.join(resource_dir(), "static", "images")


def images_dir() -> str:
    """可写图片目录：打包后为 ``<exe 同级>/images``，源码运行为 ``static/images``。

    首次启动会把内置头像 / 队徽种过去（只补缺失），之后头像更新直接写这里，
    重新编译 exe 也不会覆盖玩家已经下载过的新英雄头像。
    """
    global _IMAGES_DIR_CACHE
    if _IMAGES_DIR_CACHE:
        return _IMAGES_DIR_CACHE
    if is_frozen():
        path = os.path.join(exe_dir(), "images")
        src = _seed_images()
        if os.path.isdir(src) and os.path.abspath(src) != os.path.abspath(path):
            _copy_missing(src, path)
    else:
        path = _seed_images()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        print("[paths] 无法创建图片目录 %s：%s" % (path, exc))
    _IMAGES_DIR_CACHE = path
    return path


def images_path(*parts: str) -> str:
    """拼一个图片路径（相对 images/ 的子路径）。"""
    return os.path.join(images_dir(), *parts)
