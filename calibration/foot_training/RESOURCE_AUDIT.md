# 跑步脚部关键点与触地标注：公开资源许可审计

核查日期：2026-10-28（本机时间 UTC+08:00）。范围：仅离线第一阶段的脚跟/脚趾定位与接触事件标注；不训练综合评分。本报告是来源核查与风险分层，不是法律意见。

## 结论先行

- **COCO-WholeBody 不是可直接商用的数据标注。** 官方 README 明确限定研究和非商业使用；虽然链接显示文字漏写了 NonCommercial，实际链接指向 **CC BY-NC 4.0**。不能按显示文字误记为 CC BY 4.0。
- **CMU Foot 的两个脚部标注包明确为 CC BY 4.0，可在遵守署名等条件下商用；原图另算。** 其关联的 OpenPose / openpose_train 代码却是非商业研究许可，不能把数据许可扩展到代码或权重。
- **MMPose / RTMPose 与 rtmlib 的代码为 Apache-2.0，可有条件商用。** 这不等于所有训练数据、模型权重和再分发权均已商用清权。MMPose 的 LICENSES.md 还列有 EDPose 特殊许可（本候选不采用）。
- 作者 Tau-J 的 RTMPose 权重镜像模型卡声明 `license: apache-2.0`，是积极的权重许可证据；但非常简略，**不能据此抹除 COCO-WholeBody 非商业数据条款或替所有混合数据清权**。对目标权重的训练数据许可、商业推理/微调/再分发边界仍需权利方确认。
- 这两套数据提供**静态图像足部关键点**，不是跑步 initial contact / toe-off / GCT 的逐帧真值。RTMPose/RTMW/rtmlib 也是定位/推理工具，不是已经验证的触地事件模型。
- 本许可审计子任务仅读取公开网页、仓库文本与 HTTP HEAD 元信息，未自行下载模型或执行推理。主任务已另行下载RTMPose-s wholebody并完成7张用户报告图片的隔离预标注，详见同目录README；该运行验证不是商业授权批准。HEAD 200只证明端点响应，不等于下载或精度验收。

## 1. 分层许可矩阵

| 资源 | 图像/数据 | 标注 | 代码 | 权重 | 商用判断 |
|---|---|---|---|---|---|
| COCO-WholeBody | 沿用 COCO 2017 原图；不拥有原图版权，须逐图核查 Flickr/原作者许可 | 官方：研究与非商业；链接为 CC BY-NC 4.0 | 官方标注仓库根目录未见独立 LICENSE；评估代码称基于 COCO API，不能仅凭此替新增代码确定完整许可 | 本仓库指向 MMPose 等模型；没有由该数据 README 单独授予所有权重商用许可 | 标注商业使用须另行联系授权；原图不能整体当作 CC BY |
| CMU Human Foot Keypoint Dataset | COCO 2017 原图，单独处理图片权利 | `train2017_foot_v1.zip`、`val2017_foot_v1.zip`：CC BY 4.0 | 页面称其所提供的软件与标注适用 CC BY 4.0；关联 OpenPose / openpose_train 各自 LICENSE 为非商业研究，不能混同 | 页面指向 OpenPose；未确认独立的商业权重许可 | 两个标注包有条件可商用；图片、OpenPose 代码/权重不得随之自动放行 |
| MMPose RTMPose wholebody | 非独立数据集；基础 wholebody 使用 COCO-WholeBody，部分预训练还用 AIC/COCO/Body7 | 训练标注遵守各自原许可 | 根 LICENSE Apache-2.0；LICENSES.md 特殊算法须单查 | 官方 Model Zoo 发布可下载 PTH；作者 HF 镜像标 apache-2.0，但未见针对本 checkpoint、明确解决所有训练数据限制的完整声明 | 代码可有条件商用；目标 wholebody 权重商业清权未完成 |
| rtmlib | 不自带新数据授权，下载上游模型 | 不提供新触地真值 | Apache-2.0 | 继承具体上游模型；当前 Wholebody 默认为 RTMW Cocktail14，不是简单的 COCO17 RTMPose | 包代码可有条件商用；自动下载的检测器与姿态权重都必须单独审计 |

### 许可义务与边界

- CC BY 4.0：允许商业分享与改编；适当署名、提供许可链接、标记修改，不额外施加限制。隐私、肖像等其他权利不因此自动获得。[S5]
- Apache-2.0：允许商业使用、修改和分发，但需遵守保留许可/版权声明、修改说明及适用 NOTICE 等条件；没有替第三方图像/数据授权。[S6][S7]
- COCO 官方原始标注/网站为 CC BY 4.0，软件页面给出三条款 BSD 式条款；**COCO 原始标注许可不覆盖他人后来增加的 WholeBody 标注**。[S4]
- 研究阶段也须核实项目用途是否落入“非商业”；“本地离线”“免费试验”不自动等于非商业许可合规。

## 2. COCO-WholeBody：已证事实

官方：[仓库](https://github.com/jin-s13/COCO-WholeBody)，[README 原文](https://raw.githubusercontent.com/jin-s13/COCO-WholeBody/master/README.md)。

- 基于 COCO 2017 train/val；每人 133 点：17 身体、6 足部、68 面部、42 手部。[S1]
- 官方 Terms of Use 原句：`COCO-WholeBody dataset is ONLY for research and non-commercial use.`
- 同段链接显示 `Creative Commons Attribution 4.0 License`，但真实 href 是 `https://creativecommons.org/licenses/by-nc/4.0/legalcode`。已核查 README 原始 Markdown，不依赖会丢失链接的网页摘要。
- 商业标注授权联系：`machang[at]tetras[dot]ai`，抄送 `jinsheng13[at]foxmail[dot]com`。联系信息来自当前 README，未实际发信或确认联系人有效性。
- 原图版权不属于该团队，要求遵守 Flickr 条款。裁剪脚部不自动去除原图许可。
- `foot_kpts` 为 6 个 `(x,y,v)`；`foot_valid` 表示足部标注有效性；无效关键点可全零。不能把零坐标当真实落点。[S2]
- MMPose COCO-WholeBody 的零起始足部索引为：17 左大脚趾、18 左小脚趾、19 左跟、20 右大脚趾、21 右小脚趾、22 右跟；踝仍为 15/16。[S10] 开启 OpenPose 重排后不可照搬索引。

### 官方下载入口与小样本

| 内容 | 链接 | 大小与验证 |
|---|---|---|
| 官方评估示例 GT | https://raw.githubusercontent.com/jin-s13/COCO-WholeBody/master/evaluation/example_gt.json | HEAD 200；Content-Length **341840 bytes**；只是格式/评估小样本，不是跑步触地集 |
| 官方评估示例预测 | https://raw.githubusercontent.com/jin-s13/COCO-WholeBody/master/evaluation/example_pred.json | HEAD 200；Content-Length **344000 bytes**；预测文件不是真值 |
| 训练标注官方 Google Drive | https://drive.google.com/file/d/1thErEToRbmM9uLNi1JXXfOsaS5VK2FXf/view?usp=sharing | 官方发布页，不是已验证的裸文件直链；大小未知，未下载 |
| 验证标注官方 Google Drive | https://drive.google.com/file/d/1N6VgwKnj8DeyGXCvp1eYgNbRmw6jdfrb/view?usp=sharing | 同上；大小未知 |
| 官方列出的数据平台 | https://openxlab.org.cn/datasets/OpenDataLab/COCO-WholeBody | 平台入口；下载要求、当前大小未知 |
| COCO 原图 train2017 | http://images.cocodataset.org/zips/train2017.zip | 官方 CMU 页面写 118K / 18GB；本次未下载/测量 |
| COCO 原图 val2017 | http://images.cocodataset.org/zips/val2017.zip | 官方 CMU 页面写 5K / 1GB；本次未下载/测量 |

完整 WholeBody 标注本次没有确认可免登录裸文件下载的 URL；不把 Drive `/view` 冒充已验证直链。上面两个官方 JSON 是明确的小文件直链，但同样不能绕过 WholeBody 的使用限制。

## 3. CMU Foot：已证事实与访问阻断

官方：[项目页](https://cmu-perceptual-computing-lab.github.io/foot_keypoint_dataset/)，[页面源码](https://raw.githubusercontent.com/CMU-Perceptual-Computing-Lab/foot_keypoint_dataset/master/index.html)。

- 六点顺序（足部数组从 0 开始）：左大脚趾、左小脚趾、左脚跟、右大脚趾、右小脚趾、右脚跟。[S3]
- 页面正文写训练约 14K 标注、验证 545；下载表写约 13.5k / 0.5k。保留官方口径差异，**不是已核验的精确图像数量**。
- 页面明确两个 foot JSON zip 的标注及其所提供的软件由 CMU 所有并按 **CC BY 4.0** 授权；未出现该数据标注仅非商业的附加限制。[S3]
- 下载直链，按官方原样保留：
  - 训练：http://posefs1.perception.cs.cmu.edu/OpenPose/datasets/foot/person_keypoints_train2017_foot_v1.zip
  - 验证：http://posefs1.perception.cs.cmu.edu/OpenPose/datasets/foot/person_keypoints_val2017_foot_v1.zip
- **访问验证未通过**：两个官方 HTTP URL 的 HEAD 均返回 502；将同主机改为 HTTPS 重试均发生 TLS EOF。不能确定是源站故障、代理还是本地访问路径；不能据此断言全球失效。两个 zip 的真实大小**未知**，未下载。
- 页面所说 `2017 Train/Val annotations [241MB]` 指 **COCO 原始标注包**，不能错写成 CMU 足部包大小。
- 最小明确划分为验证约 545 标注，但仍需足部 zip 与相应图片配对；没有确认另有独立 tiny 样本包。
- 数据联系人：`mcevoy@cmu.edu` / `innovation@cmu.edu`，尚未联系。

### CMU 代码/权重的特别提醒

数据页把模型/测试代码链接到 OpenPose，把训练代码链接到 openpose_train；已读取两仓库 LICENSE，均有 `ACADEMIC OR NON-PROFIT ORGANIZATION NONCOMMERCIAL RESEARCH USE ONLY`，并限制转授权/分发等。[S11][S12]

因此：**CMU foot 标注 CC BY ≠ OpenPose 代码或模型 CC BY。** 本阶段不引入 OpenPose 权重；独立 foot detector checkpoint 的商业授权和文件大小未确认，不能用数据页的软件统称覆盖另一个仓库的实际 LICENSE。

## 4. MMPose RTMPose wholebody：模型确实存在，商用权重需另审

官方：[RTMPose 项目](https://github.com/open-mmlab/mmpose/tree/main/projects/rtmpose)，[完整 Model Zoo 原文](https://raw.githubusercontent.com/open-mmlab/mmpose/main/projects/rtmpose/README.md)。

- 官方 WholeBody 2d (133 Keypoints) 表确实列出 RTMPose-m / l / x；基于 COCO-WholeBody 的 m(256×192) Whole AP 为 58.2。[S8] 这是公开数据集总体关键点指标，不是本项目跑步脚跟误差或接触事件准确率。
- 明确候选：RTMPose-m `rtmpose-m_8xb64-270e_coco-wholebody-256x192.py`。
  - [配置直接链接](https://raw.githubusercontent.com/open-mmlab/mmpose/main/projects/rtmpose/rtmpose/wholebody_2d_keypoint/rtmpose-m_8xb64-270e_coco-wholebody-256x192.py)
  - [PTH 权重直接链接](https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/rtmpose-m_simcc-coco-wholebody_pt-aic-coco_270e-256x192-cd5e845c_20230123.pth)
  - HEAD 200；Content-Length **72010049 bytes**。未下载、未执行；PTH 是姿态权重，不包含完整人体检测/主体关联方案。
- 其他 wholebody 分支要分开：DWPose 使用 COCO-WholeBody+UBody；RTMW Cocktail14 使用 14 数据集，不应都简称为“COCO 模型”。[S8]
- 官方根代码许可 Apache-2.0；特殊许可列表当前列 EDPose IDEA License 1.0，没有在该列表为 RTMPose 单列特殊代码许可。[S6][S9]
- MMPose Issue #2106 的维护回复对 COCO 训练的两个特定模型表示据其所知可商用，并建议咨询法律人士；**该问题不是 WholeBody/RTMW 的逐权重授权函**，不能推广至 COCO-WholeBody 非商业标注。[S13]
- 作者维护的 HF 权重镜像 [Tau-J/RTMPose](https://huggingface.co/Tau-J/RTMPose) 的 [模型卡原文](https://huggingface.co/Tau-J/RTMPose/raw/main/README.md) 声明 `license: apache-2.0`。[S14] 记录这一声明，但没有下载权重检查内含 LICENSE；镜像模型卡未给出各训练集商用授权证明。

## 5. rtmlib：本地推理可选，但默认权重不是无风险“顺带依赖”

官方：[仓库](https://github.com/Tau-J/rtmlib)，[LICENSE](https://raw.githubusercontent.com/Tau-J/rtmlib/main/LICENSE)，[Wholebody 实现](https://raw.githubusercontent.com/Tau-J/rtmlib/main/rtmlib/tools/solution/wholebody.py)。

- 包代码 Apache-2.0；官方支持 ONNXRuntime 等后端，可用本地路径指定模型。不必向在线 demo 提交用户视频。[S7][S15]
- 当前 Wholebody 源码：`lightweight` 用 RTMW-m + YOLOX-tiny；`balanced` 用 RTMW-l + YOLOX-m；`performance` 用更高输入分辨率 RTMW-l + YOLOX-m。**不能假定 Wholebody 默认就是基础 RTMPose-m。**[S16]
- 官方 README 说明默认会自动下载权重，OpenMMLab 无法访问时可回退作者 HF 镜像。严格离线运行前应单独批准权重获取、固定本地文件与版本，禁止将“自动下载”混成已经离线可用。
- 可核查的 lightweight 配套直链：
  - [RTMW 姿态 SDK ZIP](https://download.openmmlab.com/mmpose/v1/projects/rtmw/onnx_sdk/rtmw-dw-l-m_simcc-cocktail14_270e-256x192_20231122.zip)：HEAD 200，**120002068 bytes**。
  - [YOLOX-tiny 检测 SDK ZIP](https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/yolox_tiny_8xb8-300e_humanart-6f3252f9.zip)：HEAD 200，**18887737 bytes**。
  - 以上大小是 ZIP 端点 Content-Length，不是解压 ONNX 大小、内存占用或安装包总大小；没有下载内容校验。
- RTMW 的 Cocktail14 清单含 COCO-WholeBody、UBody、MPII、Human-Art 等，**本次未逐一完成其全部条款审计**；配套 YOLOX 人体检测器标注为 HumanArt+COCO 训练，其训练数据和权重商业边界也未完整清权。不要仅审核 pose 而漏掉 detector。
- 可优先选“已获使用许可的具体权重 + rtmlib 本地路径”用于隔离验证，但本报告没有批准任何商业部署，也没有证明本机依赖、速度、推理准确率。

## 6. 用于本项目的建议与未确认事项

### 可采用的阶段性决策

1. **脚部定位训练资源优先调查 CMU foot 的可访问性与图片权利**：标注许可明确；先解决官方链接访问，不抓取来源不明镜像，不整包下载 COCO。
2. COCO-WholeBody 仅在用途满足非商业条款并保留来源/许可时做研究；将来商用或商业产品研发用途先联系权利方，不以作者权重镜像 Apache 标签替代数据授权。
3. 模型比较在本地隔离环境进行，优先记录具体 checkpoint、代码版本、输入尺寸、关键点布局与权重哈希；本报告只完成元信息核查，没有实际运行模型。
4. **接触事件真值另建**：在权利明确且不上传的跑步视频上逐帧标 IC/TO、左右脚、遮挡/不确定区间及帧率依据；足部坐标可辅助标注，不能自生成真值或由置信度推出“触地正确”。不训练综合评分。

### 尚未确认，必须保留为未知

- CMU HTTP 502 / HTTPS TLS EOF 的原因、恢复时间、两个 zip 的实际大小与完整性。
- 完整 COCO-WholeBody 标注包大小、免登录裸下载直链与 Drive 当前可下载性。
- 所选图片逐图商业许可、人物肖像/隐私等权利；原图裁剪/再分发与训练用途是否满足所有要求。
- COCO-WholeBody 标注的商业授权费用、范围，以及具体预训练权重商业推理/微调/再分发的权利清算。
- RTMW Cocktail14、DWPose UBody 和 YOLOX-HumanArt 的完整训练来源授权链；不能因代码开源就认定通过。
- OpenPose 关联 foot checkpoint 是否有独立额外授权；目前不能确认商用。
- 模型在用户跑步视角、运动模糊、鞋型与脚部遮挡下的真实误差；触地/离地检测准确率、接地时间误差与跨人泛化均未验证。

## 7. 一手证据索引

- [S1] COCO-WholeBody README： https://raw.githubusercontent.com/jin-s13/COCO-WholeBody/master/README.md
- [S2] WholeBody 格式： https://raw.githubusercontent.com/jin-s13/COCO-WholeBody/master/data_format.md
- [S3] CMU 数据官方页与源码： https://cmu-perceptual-computing-lab.github.io/foot_keypoint_dataset/ ； https://raw.githubusercontent.com/CMU-Perceptual-Computing-Lab/foot_keypoint_dataset/master/index.html
- [S4] COCO 官方 Terms 原文： https://raw.githubusercontent.com/cocodataset/cocodataset.github.io/master/dataset/termsofuse.htm （主网站为动态页，故读取其官方仓库原文）
- [S5] CC BY 4.0： https://creativecommons.org/licenses/by/4.0/ ；完整条款 https://creativecommons.org/licenses/by/4.0/legalcode
- [S6] MMPose LICENSE： https://raw.githubusercontent.com/open-mmlab/mmpose/main/LICENSE
- [S7] rtmlib LICENSE： https://raw.githubusercontent.com/Tau-J/rtmlib/main/LICENSE
- [S8] RTMPose/RTMW Model Zoo： https://raw.githubusercontent.com/open-mmlab/mmpose/main/projects/rtmpose/README.md
- [S9] 特殊算法许可： https://raw.githubusercontent.com/open-mmlab/mmpose/main/LICENSES.md
- [S10] WholeBody 关键点布局： https://raw.githubusercontent.com/open-mmlab/mmpose/main/configs/_base_/datasets/coco_wholebody.py
- [S11] OpenPose LICENSE： https://raw.githubusercontent.com/CMU-Perceptual-Computing-Lab/openpose/master/LICENSE
- [S12] openpose_train LICENSE： https://raw.githubusercontent.com/CMU-Perceptual-Computing-Lab/openpose_train/master/LICENSE
- [S13] MMPose 模型许可讨论（非 WholeBody 专门授权）： https://github.com/open-mmlab/mmpose/issues/2106
- [S14] 作者权重镜像许可元数据： https://huggingface.co/Tau-J/RTMPose/raw/main/README.md
- [S15] rtmlib README： https://github.com/Tau-J/rtmlib
- [S16] rtmlib Wholebody 当前权重选择： https://raw.githubusercontent.com/Tau-J/rtmlib/main/rtmlib/tools/solution/wholebody.py

以上 `main`/`master` 是核查时动态分支，不是不可变版本归档；商业接入前应固定 commit 并留存许可副本。本次未改动已有源码或未提交修改，只新建本文件及所需目录。
