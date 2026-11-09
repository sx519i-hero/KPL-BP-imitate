#!/usr/bin/env bash
# KPL 模拟 BP 教练 · 一键启动 (macOS / Linux)
set -e
cd "$(dirname "$0")"

echo "======================================================================"
echo "  KPL 模拟 BP 教练 · 一键启动"
echo "======================================================================"
echo

# ---------- 1. 找 Python ----------
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "[错误] 没有找到 Python 3.8+。"
  echo "  macOS:  brew install python3"
  echo "  Ubuntu: sudo apt install python3 python3-pip python3-venv"
  exit 1
fi
echo "[1/5] Python 已找到： $PY ($($PY -c 'import sys;print(sys.version.split()[0])'))"

# ---------- 2. 虚拟环境 ----------
# 用项目内的 .venv，避免污染用户全局 Python。创建失败则回退系统 Python。
RUNPY="$PY"
if [ -x ".venv/bin/python" ]; then
  RUNPY=".venv/bin/python"
  echo "[2/5] 虚拟环境已就绪（.venv）。"
else
  echo "[2/5] 正在创建虚拟环境 .venv（首次启动）..."
  if $PY -m venv .venv >/dev/null 2>&1 && [ -x ".venv/bin/python" ]; then
    RUNPY=".venv/bin/python"
  else
    echo "[2/5] 虚拟环境创建失败，改用系统 Python 继续。"
    rm -rf .venv 2>/dev/null || true
  fi
fi

# ---------- 3. 依赖检查 ----------
if ! $RUNPY -c "import flask, requests, bs4" >/dev/null 2>&1; then
  echo "[3/5] 正在安装依赖（首次启动需要联网，约 1-3 分钟）..."
  $RUNPY -m pip install --upgrade pip >/dev/null 2>&1 || true
  if ! $RUNPY -m pip install -r requirements.txt; then
    echo
    echo "[警告] 依赖安装失败。可以尝试："
    echo "   $RUNPY -m pip install -r requirements.txt --break-system-packages"
    echo
    exit 1
  fi
  echo "[3/5] 依赖安装完成。"
else
  echo "[3/5] 依赖已就绪。"
fi

# ---------- 4. 数据检查 ----------
NEED_SCRAPE=1
if [ -f "data/heroes.json" ]; then
  if $RUNPY -c "
import json,sys
d=json.load(open('data/heroes.json',encoding='utf-8'))
sys.exit(0 if len(d.get('heroes',[]))>50 else 1)
" >/dev/null 2>&1; then
    NEED_SCRAPE=0
  fi
fi

if [ "$NEED_SCRAPE" = "1" ]; then
  echo
  echo "[4/5] 没有找到可用的英雄数据（data/heroes.json）。"
  echo "       启动游戏后打开「数据 → 更新数据」，点「更新并抓取」即可自动抓取，"
  echo "       不需要自己运行 Python 脚本（也避免频繁请求被官方限流）。"
else
  echo "[4/5] 数据已存在，跳过检查。"
fi

# ---------- 5. 启动 ----------
# 端口已被占用时只开浏览器，不重复起服务。
if $RUNPY launcher.py port >/dev/null 2>&1; then :; else
  echo
  PORT_NOW=$($RUNPY launcher.py config_port 2>/dev/null || echo 5000)
  echo "[提示] 检测到 127.0.0.1:$PORT_NOW 上已经有本游戏在运行，直接打开浏览器。"
  $RUNPY launcher.py open >/dev/null 2>&1 || true
  exit 0
fi

echo
echo "[5/5] 正在启动服务器 ..."
PORT_NOW=$($RUNPY launcher.py config_port 2>/dev/null || echo 5000)
URL="http://127.0.0.1:$PORT_NOW"

# 后台延时开浏览器（不阻塞服务）
($RUNPY launcher.py open_delayed 3 >/dev/null 2>&1 || {
  sleep 3
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi
}) >/dev/null 2>&1 &

echo
echo "======================================================================"
HOST_NOW=$($RUNPY launcher.py config_host 2>/dev/null || echo 0.0.0.0)
echo "  浏览器会自动打开 $URL"
echo "  监听地址：$HOST_NOW:$PORT_NOW（可在网页里「设置 → 服务器访问地址」修改，改完重启生效）"
echo "  如果没有自动打开，请手动访问上面的地址。"
echo
echo "  按 Ctrl+C 停止服务器。"
echo "======================================================================"
echo
exec $RUNPY app.py
