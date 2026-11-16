# P1 本机核验结果

## 实际执行

环境：本仓库 `.venv`，Python 3.9.6。已确认此 venv 无 torch、mediapipe。

```sh
.venv/bin/python -m pytest tests/running calibration/tests/test_profile_compat.py -q
```

**25 passed in 0.38s，0 failed**。含 P1 Flask/临时 SQLite/合成 FIT/隔离 mock Garmin、
keyring mock/加密文件权限/真实认证集成，以及既有 profile 兼容性测试。

```sh
node --test tests/running/test_theme_ui.js calibration/tests/test_*ui.js
```

Node 原生汇总：**68 tests / 68 pass / 0 fail / 0 skipped**。
包含新主题切换/持久化测试与既有摄像头、认证、离线 shell、画像、结果、趋势、上传 UI 契约。
这些是 JS DOM double 测试，不是像素/真实浏览器对比度验收。

```sh
git diff --check
```

退出码 0。额外对照 `69f283d`：`app.py` 原有 **60 个函数/类 AST 完全一致**，
只有顶层注册代码新增；以下 **8 个文件与基线字节一致**：
`LICENSE`、`measurement_safety.py`、`inference_report_contract.py`、`auth.py`、
`profile.py`、`history.py`、`trend.py`、`backup.py`。

## 真实 HTTP 启动检查

用临时私有数据目录、随机本机端口运行 `.venv/bin/python -m running.preview`：

- `/api/health` → 200，`mode="P1-only preview"`、`pose_inference=false`。
- `/`、`/activities.html`、`/garmin.html`、`/settings.html`、`/themes/theme-system.js` → 200。
- 匿名访问 `/api/running/activities` → 401。
- 验证后已终止预览进程并自动清除本次临时数据目录。

## 边界与未执行项

- 未持有、保存或发送任何真实佳明凭证；mock 成功不代表佳明真实端点实连成功。
- 未安装重型姿态依赖、下载模型、运行跑姿测量或调用本地 LLM。
- 未运行全仓推理/报告/生产沙箱测试；轻量预览不是完整 app.py 的模型环境启动验证。
- 未操作 NAS 生产，也没有进行 NAS 镜像构建或容器测试。
- 真实设备 FIT、真实系统 keyring、佳明端点/撤销/频率限制、三主题逐屏视觉无障碍和
  NAS 沙箱完整回归仍需进一步核验。细节与预览步骤见 `RUNNING-P1.md`。
