# 参与贡献

这个项目最欢迎的贡献不是"加一个更难的排行榜"，而是**让测量更可信**。

## 最有价值的四类贡献

### 1. 报告一个测量缺陷（最高优先级）

如果你发现"某个模型表现异常差"，而查下去是测量代码的问题，请开 issue 并附上：

- 复现命令（哪一族、哪个 seed、什么配置）
- 期望行为 vs 实际行为
- 原始证据（`runs/<时间戳>/runs.jsonl` 里的那一条，可以脱敏）

**每条确认的缺陷都会进 [`docs/MEASUREMENT_DEFECTS.md`](docs/MEASUREMENT_DEFECTS.md) 并补一个回归测试。**

### 2. 加一族新任务

要求（缺一不可）：

1. 题目运行时程序化生成，答案由参考实现**现场算出**（不能用固定答案表）
2. 判定优先用代码 oracle；只有开放文本才允许盲评
3. 埋至少一个"同型陷阱"（换个约束条件答案就完全不同），挡住背题型
4. 在 `aipk/oracle.py::truth_answer` 加一条 → 让 `selfcheck` 能验证"真值不会被判错"
5. 在 `tests/test_measurement.py` 加反套利测试（至少：明显错的答案必须判错）
6. 三条门禁全绿：`selfcheck` / `test_measurement.py` / `demo`

### 3. 接一个真实 agent CLI（Phase B）

`aipk/harness.py::ExternalCLI` 支持换命令。要求：CLI 能把"最后一条消息"写进文件
（`-o`）或在工作区写 `ANSWER.json`，且能非交互运行。附上 `runs/HARNESS_COMPARE.md` 风格的对照结果最受欢迎。

### 4. 让裁判更可信

- 更好的 rubric、更省钱的裁判组合（我们实测 glm-5.3 在长提示下慢到几分钟一条）
- **人类标注锚点**：哪怕 50 条人工标注，也能校准裁判偏差 —— 这比加更多裁判有用
  （*Nine Judges, Two Effective Votes* 已证明加裁判收益极低）

## 开发约定

```bash
python -m aipk selfcheck            # 必须绿
python tests/test_measurement.py    # 必须绿
python -m aipk demo                 # 必须绿（离线，不需要 key）
```

- 改代码前先读 [`CODEX.md`](CODEX.md) 的"铁律与踩过的坑"一节
- 跑真模型**必须**加 `--qps 1.2`（不限速会被网关 429 打成假 0 分）
- 别同时跑两个 run
- 结论措辞请保持："在这次任务分布和配置下" —— 不写"模型 X 更强"
- 提交信息用中文或英文都行，但**改了什么、为什么改**要写清楚

## 不要做的

- 不要提交密钥、`aipk.config.yaml`、`runs/` 目录
- 不要把"更难的同类题"当成主要贡献方向 —— 正确率已全面饱和，
  加难度不会增加区分度（[`docs/FINDINGS.md`](docs/FINDINGS.md) 有实测）
