#!/usr/bin/env bash
# 下载 YOLO11n-Pose 模型权重到 data/models/（约 6MB，Ultralytics 官方发布，AGPL-3.0）。
# 仓库不内置权重文件；本地运行前执行一次即可。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/models
URL="https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-pose.pt"
OUT="data/models/yolo11n-pose.pt"
if [ -s "$OUT" ]; then
  echo "已存在: $OUT"
  exit 0
fi
echo "下载 $URL ..."
curl -fL --retry 3 -o "$OUT" "$URL"
echo "完成: $OUT ($(du -h "$OUT" | cut -f1))"