# PoseRun 跑姿分析系统
# 基础镜像与生产一致：Python 3.12
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgl1/libglib2.0-0: OpenCV 运行库；fonts-noto-cjk: 报告中文渲染（开源字体）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 fonts-noto-cjk curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 模型权重（约6MB，Ultralytics 官方发布）。如构建环境无法访问 GitHub，
# 可注释掉此步，改为运行时挂载: -v /path/yolo11n-pose.pt:/app/data/models/yolo11n-pose.pt
RUN mkdir -p data/models \
    && curl -fL --retry 3 -o data/models/yolo11n-pose.pt \
       https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-pose.pt

RUN mkdir -p data uploads reports keyframes

EXPOSE 8080
VOLUME ["/app/data", "/app/uploads", "/app/reports"]

# 首次启动且用户库为空时，种子管理员 admin/admin123（可用 ADMIN_USERNAME/ADMIN_PASSWORD 覆盖）。
# 登录后请立即修改密码！生产环境建议注入 AUTH_SECRET 与 SECRET_KEY。
CMD ["python", "app.py"]