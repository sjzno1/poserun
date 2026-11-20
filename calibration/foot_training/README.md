# 脚部定位第一阶段（离线，不影响线上）

## 当前确实完成的内容

- 独立运行RTMPose-s COCO-WholeBody 133点ONNX模型，使用OpenCV CPU后端；未向生产环境安装任何新依赖。
- 对用户报告中的7张裁剪图运行真实模型预标注，提取每图10个膝/踝/脚趾/脚跟候选点。低置信度、非有限值和越界点保持未知。
- 完成单文件离线人工复核页面：点击修正、标未知、复核者、逐图确认、修改撤销确认及JSON导出，无CDN/请求/上传。浏览器真实交互验证了翻页、复核人必填、修改撤销、未知null、导出不带图片和训练授权仍关闭。
- 标注契约测试12项通过；既有calibration/tests回归160项，159通过1跳过。
- 实际执行训练准入检查，7份预标注被正确拒绝，原因是training rights not cleared。所有reviewed仍为false。没有训练任务、没有新训练权重、没有对外发布模型、没有改变线上报告能力。

**这7张图不是连续视频，不是触地事件数据，不是独立测试集，不是人工真值；不能据此计算步频、接地时间或模型准确率。** 全图人物裁剪是显式给定ROI，不是自动选人/跟踪验证。

## 资源与阻断

模型由MMPose官方项目模型表链接下载：
https://github.com/open-mmlab/mmpose/tree/main/projects/rtmpose

实际zip：31,357,652字节，SHA256 `4608671d47b7693a55601cbac109cbdd612553d7864666292b955c62e14ad66a`。
实际ONNX SHA256 `b791032a77010398f95b112f863dbe30ad16e09ff156753fb08484ff5bf8f718`。

CMU足部验证集标注：官方HTTPS端点TLS失败，HTTP回退502；没有捏造下载成功或人工标注。资源许可需区分代码、权重、标注、原图，具体审计见`RESOURCE_AUDIT.md`（独立核查任务生成）。不默认网络素材可训练或可商用，不抓取大规模赛事视频，不使用原来的精英赛事离线样本作训练集。

## 许可核查后的执行边界

已直接核对COCO-WholeBody官方README：标注仅限研究和非商业，链接实际为CC BY-NC 4.0。不能把前述离线运行成功当作商业用途许可。现有wholebody权重的训练数据与商业使用授权链尚未确认，因此不把该候选移入生产，也不以它继续开展用途未明确的训练或生成生产训练标签；已有实验产物保留为明确标记的待清权研究记录，不作为获准训练集。

CMU足部标注包明确CC BY 4.0，但原图与代码许可必须分别核查；下载仍被官方端点访问问题阻断。优先继续核实CMU资源获取与逐图许可，或使用用户明确同意训练且权利完整的自有原片。接触事件仍需独立人工连续标注，单张报告图片不能替代。

## 文件与复现

源码：`calibration/foot_training/`。
私有模型、图像与预标注：`data/offline-foot-training/`，已被项目data/规则排除Git，目录权限700。

```sh
# 本次复用了本机Python3.9现有cv2/numpy，仅在独立scratch venv添加rtmlib。
python3 -m venv --system-site-packages "$HOME/.cache/poserun-foot-prelabel-venv"
"$HOME/.cache/poserun-foot-prelabel-venv/bin/python" -m pip install --no-deps rtmlib==0.0.15

# 有大小限制、来源与SHA256记录；CMU官方失效时记录blocked而非假数据。
python3 calibration/foot_training/download_resources.py

# 使用本地模型和显式人物裁剪图；不联网推理。
"$HOME/.cache/poserun-foot-prelabel-venv/bin/python" \
  calibration/foot_training/prelabel.py \
  --model data/offline-foot-training/rtmpose_s_wholebody/20230831/rtmpose_onnx/rtmpose-s_simcc-ucoco_dw-ucoco_270e-256x192-3fd922c8_20230728/end2end.onnx \
  --images /path/to/authorized-person-crops/*.png \
  --output data/offline-foot-training/my-prelabels

python3 -m unittest discover -s calibration/foot_training -p 'test_*.py' -v
python3 calibration/foot_training/annotation.py data/offline-foot-training/report-image-prelabels/prelabels.json
# 下一条应拒绝未授权、未复核、未分组的预标注，不是训练命令。
python3 calibration/foot_training/annotation.py data/offline-foot-training/report-image-prelabels/prelabels.json --for-training
```

打开`data/offline-foot-training/report-image-prelabels/review.html`可在本机逐点复核，导出的JSON需另行保存。当前没有导入旧复核JSON/自动保存功能，刷新前须导出；关闭页面会丢失未导出的编辑。本轮未搭建公开标注服务。

完整协议见`ANNOTATION_PROTOCOL.md`。下一步需要权利清楚的连续跑步原片、经过确认的采集/播放时间信息、人工复核及独立留出集，才进入视频事件标注和微调。训练与评价执行器尚未实现，不把预标注当成训练完成。
