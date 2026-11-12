# PoseRun 跑姿分析系统

PoseRun 是一个自托管的跑步姿态（跑姿）视频分析系统：上传跑步视频，基于 **YOLO11n-Pose 关键点检测 + LSTM 时序平滑** 提取二维人体关键点，计算膝盖角、小腿角、跨步、对称性、身体前倾等几何指标，并生成**证据安全**的分析报告（PDF/DOCX）与关键帧标注。

当前版本：**v2.8.0**（见 `release_info.py`）。

> ⚠️ **默认凭据警示**：全新安装首次启动会种子管理员账号 **`admin / admin123`**。
> 这是公开的默认值，**登录后请立即修改密码**（界面右上角用户菜单 → 修改密码），
> 或在首次启动前用环境变量 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 覆盖。
> 种子仅在用户库为空时执行一次，不会覆盖已有账号。

## 功能

- **视频跑姿分析**：YOLO11n-Pose（COCO 17 关键点）+ LSTM 平滑，单人锁定、逐帧几何测量
- **证据安全报告**：只报告可测量事实与覆盖率；证据不足的分项标注「未知／未验证」，不输出评分、不做医学诊断（两页式评测报告 + 关键帧图注）
- **多用户与权限**：管理员/教练/学员角色，SQLite 账号库，PBKDF2 口令哈希，令牌认证与限流
- **历史与趋势**：分析历史存储、指标趋势、A/B 对比、训练建议联动
- **实时分析**：摄像头实时关键点预览（需 HTTPS 安全上下文）
- **自动备份**：启动即备份 + 每日循环备份数据目录
- **Web 界面**：内置静态前端（Bootstrap），上传 → 分析 → 报告全流程

## 快速开始

### 方式一：Docker（推荐）

```bash
# 构建（镜像内自动下载 YOLO11n-Pose 权重，约6MB）
docker build -t poserun:2.8.0 .

# 运行（首次启动种子 admin/admin123，登录后立即改密）
docker run -d --name poserun \
  -p 8080:8080 \
  -v poserun-data:/app/data \
  -v poserun-uploads:/app/uploads \
  -v poserun-reports:/app/reports \
  -e ADMIN_PASSWORD='换一个强密码' \
  -e AUTH_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
  poserun:2.8.0
```

浏览器访问 `http://localhost:8080`。摄像头实时页面需要 HTTPS：可在容器内放置
`certs/server.crt` 与 `certs/server.key`（应用会自动在 8443 端口启用 HTTPS），
或在前端用 Nginx/Caddy 等做 TLS 反向代理。

### 方式二：本地运行

要求 Python 3.10–3.12（生产容器为 3.12）。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 下载模型权重（仓库不内置 .pt 文件）
bash scripts/download_model.sh

# 可选：纯 CPU 环境先装 CPU 版 torch 再装其余依赖，可显著减小体积
# pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

python app.py
```

访问 `http://localhost:8080`，用 `admin / admin123` 登录并**立即修改密码**。

## 配置（环境变量）

所有变量均可选，完整示例见 [`.env.example`](.env.example)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `admin123` | **仅全新安装首次启动**的种子管理员；已有用户库不受影响 |
| `AUTH_SECRET` | 首次启动随机生成，存 `data/.auth_secret`（600） | 会话令牌签名密钥；多实例/重建后保持登录态请显式注入 |
| `SECRET_KEY` | 占位符（生产必改） | Flask 会话密钥 |
| `CORS_ORIGINS` | `http://localhost:8080,http://127.0.0.1:8080` | 允许跨域的来源，逗号分隔；生产设为你的域名 |
| `POSERUN_DATA_DIR` | `./data` | 账号库/历史数据目录 |
| `POSERUN_UPLOADS_DIR` / `POSERUN_REPORTS_DIR` | `./uploads` / `./reports` | 上传视频 / 生成报告目录 |
| `POSERUN_BACKUP_DIR` | `<data>/.backups` | 自动备份目录 |
| `AUTH_DB_PATH` | `<data>/auth.db` | 账号库 SQLite 路径 |
| `MAX_CONTENT_LENGTH` | `52428800`（50MB） | 上传体积上限（字节） |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | 监听地址（`python app.py` 入口） |

**密钥说明**：本仓库不包含任何生产密钥。令牌签名密钥默认随机生成并持久化在
数据目录（已被 `.gitignore` 排除）；`.env`、`certs/`、`*.db`、`.auth_secret`
均不入库。

## 模型权重与字体

- **权重**：`data/models/yolo11n-pose.pt` 不随仓库分发，用 `scripts/download_model.sh`
  从 Ultralytics 官方 release 下载（AGPL-3.0）。缺失时分析接口会明确报错，不会静默降级。
- **中文字体**：报告中文渲染优先使用开源 Noto CJK（Docker 镜像已内置
  `fonts-noto-cjk`）；`cn_font.py` 中的 macOS 系统字体路径仅供本地开发回退，
  仓库不捆绑任何商业字体文件。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q          # 后端契约/回归/发布加固：56 项
pytest calibration/tests/ -q \
  --ignore=calibration/tests/test_cadence_candidate.py \
  --ignore=calibration/tests/test_overlap_evidence.py
```

- `tests/`：后端契约、回归、发布加固、worker 等测试；部分端到端用例需要真实模型权重。
- `calibration/`：验收脚本与测试（真实视频管线、报告契约、生产安全检查等），
  部分脚本需要真实跑步视频素材，缺素材时会明确跳过/报错，不会伪造结果。
- 已知遗留问题（v2.8.0 源码自带，非发布整理引入）：
  - `test_cadence_candidate.py` / `test_overlap_evidence.py` 引用的实验模块未随 v2.8.0 发布，
    收集会报错，上面命令已用 `--ignore` 排除；
  - `calibration/test_measurement_contract.py` 中 4 项测量契约断言与当前实现不一致
    （画布纵横比无关性、LSTM 平滑确定性），在原始源码上同样失败，待后续版本修复；
  - NAS 沙箱相关的 9 项用例在普通环境自动跳过。

## 目录结构（节选）

```
app.py                 Flask 应用入口（API + 静态前端）
auth.py                认证/角色/令牌/管理员种子
pose_analyzer.py       YOLO11n-Pose + LSTM 关键点后端
calibrated_analyzer.py 校准分析器（几何指标）
gait_analyzer.py       步态周期分析
camp_report.py         两页式评测报告（PDF/DOCX，证据安全）
report_generator.py    通用报告生成
template_report.py     样板排版报告（含虚构示例数据）
history.py / trend.py  历史存储与趋势
backup.py              自动备份
config.py              配置
static/                前端页面与资源
calibration/           验收脚本与测试
tests/                 单元/集成测试
```

## 许可证

**本项目以 AGPL-3.0 发布（见 `LICENSE`）。** 选择依据：核心依赖 ultralytics
与 PyMuPDF 均为 AGPL-3.0，组合作品须整体以 AGPL-3.0 发布（网络服务亦触发
源码公开义务）。各依赖许可证影响如下：

| 依赖 | 许可证 | 影响 |
| --- | --- | --- |
| **ultralytics（YOLO11）** | **AGPL-3.0** | 强 copyleft：组合/衍生作品整体须以 AGPL-3.0 发布（或购买 Ultralytics 商业授权） |
| **PyMuPDF（fitz）** | **AGPL-3.0** | 同上（或购买 Artifex 商业授权） |
| torch / torchvision | BSD-3 | 宽松 |
| OpenCV | Apache-2.0 | 宽松 |
| Flask / Werkzeug / Jinja2 | BSD-3 | 宽松 |
| numpy | BSD-3 | 宽松 |
| Pillow | MIT-CMU（HPND） | 宽松 |
| reportlab | BSD-3 | 宽松 |
| python-docx | MIT | 宽松 |
| flask-cors / flask-limiter | MIT | 宽松 |

**说明**：模型权重 `yolo11n-pose.pt` 同样来自 Ultralytics（AGPL-3.0），
构建 Docker 镜像时从官方发布地址下载，不随本仓库分发。若需闭源商业分发，
须先取得 Ultralytics 与 Artifex 的商业授权或替换相应依赖。

**商标声明**：「Pose Method®／姿势跑法」是 Romanov 博士及其授权机构的注册商标。
本项目是独立的生物力学几何测量工具，与上述商标持有人无隶属或授权关系；
报告中的技术描述仅作教练参考，不构成官方认证。

## 隐私与安全

- 视频与报告仅存储在你自己的服务器/容器内，分析过程不调用任何外部 API。
- 生产部署请：修改默认管理员密码、注入 `AUTH_SECRET`/`SECRET_KEY`、
  设置 `CORS_ORIGINS`、使用 TLS 反向代理、定期备份 `data/` 卷。
- 本工具输出为二维几何测量，**不是医学诊断**；伤病问题请咨询专业人员。