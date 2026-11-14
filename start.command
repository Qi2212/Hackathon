#!/bin/bash
# CampusGrove (Aurora v3) 一键启动（macOS）
cd "$(dirname "$0")"
set -e
if [ ! -d .venv ]; then
  echo "[1/3] 创建虚拟环境..."
  python3 -m venv .venv
fi
source .venv/bin/activate
echo "[2/3] 安装依赖（清华镜像，失败自动换官方源）..."
pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
  || pip install -q -r requirements.txt
echo "[3/3] 启动服务：http://localhost:8080  （Ctrl+C 停止）"
( sleep 2 && open http://localhost:8080 ) &
python run.py
