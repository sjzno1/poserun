# 权重获取、SHA-256 校验与许可门禁流程（foot_training）

状态：**blocked**。本轮（2026-11-13，accuracy-prep 节点）未下载任何权重、未执行模型推理。
本文件是恢复进权威源码树的流程文档；下载源与哈希以 README.md / RESOURCE_AUDIT.md / download_resources.py 的既有记载为准。

## 1. 前置许可门禁（任一未过即不得下载/使用）

- G1 用途界定：COCO-WholeBody 官方 README 明确「ONLY for research and non-commercial use」，链接显示文字虽漏写 NonCommercial，实际 href 为 CC BY-NC 4.0（RESOURCE_AUDIT 已核对原始 Markdown）。在取得权利方书面授权或用途确认落入非商业条款之前，相关权重与标注**不得用于生产训练标签、不得上线、不得再分发**。商业标注授权联系（据 RESOURCE_AUDIT，未验证有效性）：machang[at]tetras[dot]ai，抄送 jinsheng13[at]foxmail[dot]com。
- G2 权重训练数据链：RTMPose wholebody 权重基于 COCO-WholeBody（部分含 AIC/COCO/Body7）；作者 HF 镜像模型卡 apache-2.0 声明不能抹除数据侧 BY-NC 条款。MMPose/rtmlib 代码 Apache-2.0 不自动解决权重/训练集商用边界。
- G3 CMU foot 标注包为 CC BY 4.0，但 COCO 原图须逐图清权；关联 OpenPose / openpose_train 代码为非商业研究许可，不得随数据放行。官方端点 2026-10-28 实测 HTTP 502 / HTTPS TLS EOF，访问阻断未解决。
- G4 优先路径：用户明确同意训练且权利完整的自有跑步原片（见素材需求清单）。
- G5 禁止 rtmlib Wholebody 默认自动下载：其默认权重为 RTMW Cocktail14 混合数据（14 数据集，含 UBody/HumanArt 等未逐一审计来源）+ YOLOX 检测器，均须单独清权。只允许显式本地 --model 路径。

## 2. 下载源（仅以下记载来源；不使用未审计镜像）

| 资源 | URL | 记载状态 |
|---|---|---|
| RTMPose-s wholebody ONNX SDK zip | https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/rtmpose-s_simcc-ucoco_dw-ucoco_270e-256x192-3fd922c8_20230728.zip | README 记载曾下载成功并哈希校验；文件现缺失 |
| CMU foot 验证标注 | https://posefs1.perception.cs.cmu.edu/OpenPose/datasets/foot/person_keypoints_val2017_foot_v1.zip | 官方端点 502/TLS EOF，blocked |

执行器：download_resources.py（128MiB 单文件上限、256MiB 解压上限、zip 路径穿越检查、来源/字节数/SHA-256 写入 download-manifest.json；失败记 blocked 而非假数据）。本轮未运行该脚本。

## 3. SHA-256 校验（文档记载值，2026-10-28 实测）

- zip：31,357,652 字节，SHA-256 4608671d47b7693a55601cbac109cbdd612553d7864666292b955c62e14ad66a
- 解压后 end2end.onnx：SHA-256 b791032a77010398f95b112f863dbe30ad16e09ff156753fb08484ff5bf8f718

流程：下载 → 重算 zip SHA-256 与字节数 → 解压 → 重算 ONNX SHA-256 → 全部一致才放行；任一不符即隔离文件并记录，不得投入使用。
记载中的存放路径（相对项目根）：data/offline-foot-training/rtmpose_s_wholebody/20230831/rtmpose_onnx/rtmpose-s_simcc-ucoco_dw-ucoco_270e-256x192-3fd922c8_20230728/end2end.onnx。data/ 为私有目录（700、排除 Git）。
注意：权威源码树 work/ 不含 data/offline-foot-training/；退役归档中的私有数据不在本轮复制范围 → 权重与 7 份历史预标注记录均缺失，需按上述流程重新获取。

## 4. 运行与训练准入门禁（代码已实现并经机制测试）

1. prelabel.py --model <显式本地路径> --images <授权裁剪图> --output <目录>：文件不存在立即 ValueError: all model/image files must already exist locally（先于 cv2/rtmlib 导入，无网络请求）；run.json 记录模型 SHA-256；133 点原生布局（to_openpose=False），禁 OpenPose 重排索引。
2. 静态图契约：contact 左右均 unknown、events 为空；低分/非有限/越界点 visibility=0 且坐标 null；proposal_score 不是准确率。
3. 人工复核：review.html 逐点修正、复核者必填、修改即撤销 reviewed；导出 JSON 不含图片。**机制测试通过不等于人工复核完成。**
4. annotation.py <prelabels.json> --for-training：要求 rights.training_allowed=true 且 basis 非空、reviewed=true 且 reviewer 非空、subject_id/session_id、显式 train/validation/test 分组；拒绝重复内容与跑者/场次跨集合泄漏。本轮合成 fixture 实测被拒：ValueError: training rights not cleared。
5. 训练执行器尚未实现；通过准入校验不等于已训练、准确或可部署。

## 5. 当前阻塞摘要

权重缺失（本地无 ONNX）；BY-NC/商用授权链未清；CMU 端点阻断；无连续跑步视频；7 份历史预标注 rights-not-cleared 且 reviewed=false（且文件不在权威树）；无跑者/场次分组与独立留出集。详见 accuracy-prep handoff 阻塞登记册。
