# 跑步管理系统 P1（本机增量版）

基线：poseRun v2.8.0 / `69f283d`，许可证仍为 AGPL-3.0。

## 最小依赖与本机预览

```sh
cd /Users/sjz/projects/poserun
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-running.txt
.venv/bin/python -m running.preview
```

打开 http://127.0.0.1:5055 。预览复用真实 `AuthManager`，可在登录框注册测试账号。
`RUNNING_PREVIEW_DATA` 默认 `data/running-preview`，与原系统数据库分开；
`RUNNING_PREVIEW_PORT` 可更改端口。只监听 loopback，不启用 Flask debug。
这个入口只服务 P1 路由和静态页面，**不能执行旧跑姿 API**；跑姿入口保留，但在
此轻量预览中请求旧 API 会得到 404。已有全依赖环境仍用原 `app.py` 启动，
它增量注册同一组蓝图，不修改任何旧跑姿路由/报告函数。
没有安装/导入 torch、mediapipe，不下载模型，不请求任何本地 LLM，不操作 NAS。

## 功能与数据边界

- clarion / nightlab / fieldnote 令牌来自设计原型。所有使用 common.js 的旧页面
  也加载共享主题覆盖；顶栏与设置页即时切换，localStorage 保存即时偏好。
  登录后以 `profiles.db` 新表 `theme_preferences` 的当前用户偏好为准，旧 profiles
  表结构不变。P1 为共享结构上的基础布局变体，不宣称完成 P4 全屏视觉验收。
- 活动：`/api/running/activities`，`/<id>`，`/import`（POST multipart `file`）。
  要求单 session FIT、合法 CRC、≤10 MiB；保存 UTC 摘要和 SHA-256，不保存 GPS 原文件。
  按用户隔离；按哈希/佳明 ID/相同开始时间+运动+距离/时长误差≤1 去重。
  多 session FIT 明确拒绝，不静默丢掉其余运动。列表支持 limit/offset。
- 总览：`/api/running/dashboard`，UTC 周一至周日跑量，引用活动 ID。
  缺距离则总量未知而不是补零；无记录为 0 km。骑行与 demo 不计入跑量。
  负荷图始终显式标「演示、未核验、非本人、a.u.」，不打分或诊断。
- 佳明：GET/DELETE `/api/running/garmin`、POST `/connect`、POST `/sync`。
  demo 无 key、无网络，活动 source=`demo`；真实模式默认关闭，显式设置
  `GARMIN_LIVE_ENABLED=1` 才允许发送 key 至固定 Garmin token 端点。
  测试调用成功才加密保存，响应、错误与日志不回显 key/token；不接受账号密码。
  同步为用户手动刷新最近 100 条摘要并幂等入库，**不是自动调度、全量历史回填**；
  健康分片、自动调度、API FIT 下载、细分跑步类型归类仍待后续阶段。
  过期阻止同步，30 天内提示续期；有效期由用户声明，非 Garmin 验证结果。
  断开删除连接与同步状态，保留已导入活动。删除账号级联清除活动与连接。

## 加密与备份

key 本身通过 Fernet 认证加密保存于 `running.db`；主密钥优先从
`GARMIN_KEY_SECRET`（有效 Fernet key）读取，否则尝试系统安全 keyring
(macOS / SecretService / Windows)。不可用时生成 `garmin-vault.key`（600），
数据库也设为 600。不使用明文 keyring 后端。token 只在客户端对象内存中短暂存在。
不要把真正凭证粘贴到聊天或提交到 Git；通过本机受信任页面输入。

降级文件与数据库同目录，**只能防止单独数据库泄漏，不能抵御整机失陷或整份备份泄漏**。
现有 backup.py 递归处理 data/ 下 SQLite（backup API），因此 running.db 自动纳入；
备份会含文件降级主密钥，必须将完整备份当敏感资料。系统 keyring 主密钥不会自动进入
备份；迁移需显式安全迁移主密钥。部署建议使用独立 `GARMIN_KEY_SECRET`，离线备份并
保持密钥来源稳定；丢失主密钥时应断开并重新连接，而非静默重置密文。

## 佳明协议参照（非真实 key 验证）

- 官方 Swagger： https://developer.garmin.com/gc-developer-program/personal/swagger-docs/
- 参照实现： https://github.com/indaco/garmin-personal-mcp/blob/main/src/api-client.ts
- 活动字段与路径： https://github.com/indaco/garmin-personal-mcp/blob/main/src/tools.ts
- 固定主机 `https://personal.connectapi.garmin.com`
- token：POST `/personalapitoken-service/personal-api/personal-api-key/token`，
  `Authorization: ApiKey ...`；活动 GET 使用 `Authorization: Bearer ...`
- 活动：GET `/activitylist-service/activities/search/activities?start=0&limit=100`
- 429/5xx 最多 3 次指数退避；401 最多刷新 token 后重试一次；拒绝重定向，
  防止 key/token 被带到第三方。格式只校验可打印 ASCII 与无空白，不臆造 key 前缀。

**端点待真实 key 实测**：token 字段/TTL、活动响应结构、频率限制、回溯深度、
地区可用性均未用真实凭证验证；UI 保持提示，不将 mock 成功描述为佳明实连成功。

## 测试

```sh
.venv/bin/python -m pytest tests/running calibration/tests/test_profile_compat.py -q
node --test tests/running/test_theme_ui.js calibration/tests/test_*ui.js
```

测试使用临时 SQLite、真实 Flask test client、带 CRC 的合成二进制 FIT、mock Garmin
transport/客户端；不触碰系统真实 keyring。Node 使用实际 JS 的 DOM double，
验证切换、偏好、同步与已有 UI 契约，**不代替真实浏览器像素/对比度测试**。
全量推理/测量测试不在此环境执行，既有测试中依赖重模型和已知缺失模块的部分留待
下一阶段专用环境，不能将本页命令通过解读为全仓测试通过。

## 待进一步核验

1. 用真实 Personal key 验证端点/响应/重试/撤销/到期，当前只验证 mock 表面。
2. OS keyring 在目标桌面/容器的实际可用性、迁移和备份恢复；当前测 fallback 和隔离 mock。
3. 实际设备 FIT 样本、多品牌/字段缺失/损坏文件扩展；当前是合成 FIT。
4. 三主题在真实浏览器的移动端/旧报告页、可访问性和对比度逐屏验收。
5. NAS 镜像构建与沙箱全套跑姿契约测试（本阶段未运行、未动生产）。
6. 后台自动同步、健康数据/负荷模型/建议、完整原始 JSON/FIT 留存和 P4 精修。

新增依赖包元数据许可：fitparse MIT，keyring MIT，cryptography Apache-2.0 OR BSD-3-Clause；
仓库 AGPL-3.0 不变。生产镜像仍需按实际解析出的依赖树复核许可证和漏洞。
