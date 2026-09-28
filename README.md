# AI PK — 自己搭的模型测量台

> 官方跑分高，用起来却拉胯。这个项目不信任何榜单，**只信能机器验证的任务 + 能落盘的证据**。
>
> 它同时是一份**"我自己怎么测错"的公开记录**：18 个测量缺陷，每个都配了回归测试。

```bash
pip install -r requirements.txt   # 只有 httpx + pyyaml
python -m aipk selfcheck          # 13 个任务族的 oracle 自洽
python -m aipk demo               # 离线跑通全链路，不需要任何 API key
```

---

## 这是什么（一句话）

**程序化生成任务（防污染）→ 冻结协议下多轮 agent 执行（harness 无关）→ 代码 oracle 判定（不靠裁判）→ 体验指标量化（延迟/成本/一致性/恢复）→ 带置信区间的报告。**

13 个任务族、约 900 次真模型运行、Phase B 真实 CLI 对比、多裁判盲评审计 —— 全部证据可复算。

## 为什么值得一看（三条实测结论）

### 1️⃣ 正确率维度已经饱和 —— 连"非确定性维度"也一起饱和了

| 实验 | 规模 | 结果 |
|---|---|---|
| 旧 7 族 PK | 630 次运行 | 成功率 90.5%~100%，**95% CI 两两全部重叠** |
| 新 3 族（前提辨识 / 最小 diff / 长会话一致性） | 270 次运行 | **9/10 模型 100%** |
| 输出方差（`temperature=0.7`） | 180 次运行 | 正确性翻转 **0%**、实质漂移 **0%** |

**推论：凡是"代码可验证的清晰单一目标任务"，前沿模型基本都会做。再造更难的同类题是错方向。**

### 2️⃣ 真正的区分度在"体验"和"机制"上

同一批 100% 正确率的模型之间：

| 指标 | 最快/最省 | 最慢/最费 | 倍数 |
|---|---|---|---|
| P90 总耗时 | 57.5s | 702.2s | **12.2×** |
| 单任务 token | 5,088 | 19,326 | **3.8×** |
| reasoning 占比 | 12.2% | 98.4% | **8×** |

**这就是"跑分相同、用起来天差地别"的量化证据** —— 官方跑分永远不会给你这些数字。

### 3️⃣ harness（壳子）是被隐藏的变量，而且很贵

同一批题、同一模型族，两种壳子各跑一遍（同题 6 组配对）：

| 壳子 | 成功率 | 平均 token | 平均耗时 |
|---|---|---|---|
| 冻结协议 | 100% | **7,974** | **57.1s** |
| Codex CLI（真实 agent CLI） | 100% | **46,161（5.8×）** | **164.6s（2.9×）** |

**质量打平、成本差近 6 倍。** 独立印证：Claw-SWE-Bench (2026) 实测
harness 选择能让 Pass@1 变动 **27.4pp**（模型选择 29.4pp）。

## 那它作为榜单能用吗？—— 不能，这点必须说清楚

- 正确率饱和；唯一还有区分度的开放式质量要靠 LLM 裁判
- 而**裁判噪声大到会改变名次**：我们实测裁判之间平均 **Kendall τ = 0.467**、
  实战位置翻转率 **27.2%**；同一份答案在不同裁判手里能是 0.17 或 1.00
- 这不是个例：*Nine Judges, Two Effective Votes*（2026）证明 9 个裁判只提供约 **2 票**独立信息

**所以本项目的定位是「测量台 + 回归测试」，不是「排行榜」**：
它回答"它多快、多贵、多稳、换个壳子差多少"，不回答"谁最聪明"。
完整论证见 [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md)。

## 最有复用价值的部分：18 个测量缺陷

> 共同点：**每一次"某个模型表现异常差"，查下去都是我的测量代码有问题。**

- 不限速被 429 打成假 0 分 → GLM-5.3 被误判 5/24
- 用字符串比 JSON → **10 个模型全被冤枉**
- 题目 builder 自己给错答案 → 模型答对反被判错
- 裁判"没判出来"被记成平局 → **裁判罢工反而白送半分**
- 多裁判重评的判决键少了模型维度 → 分数全一样、τ 假模假样 =1.000

全部 18 条 + 修法 + 通用规则：[`docs/MEASUREMENT_DEFECTS.md`](docs/MEASUREMENT_DEFECTS.md)

## 快速开始

```bash
python -m aipk selfcheck                    # oracle 自洽：对的不能被判错
python -m aipk demo                         # 离线全链路（满分机器人 100%、错答 0%）
python -m aipk preview --family repofix     # 看一道题长什么样
python -m aipk init                         # 生成配置模板，接你自己的网关
python -m aipk run --reps 3 --tasks-per-family 3 --qps 1.2 --workers 6   # 正式 PK
python -m aipk report --run runs/<时间戳>    # HTML + Markdown + summary.json
```

**接自己的网关只要一个 yaml**（任何 OpenAI 兼容端点）：

```yaml
providers:
  mygateway:
    base_url: https://api.example.com/v1
    api_key_env: MY_GATEWAY_API_KEY
roster:
  - [mygateway, strong-model, "某旗舰", flagship]
```

详见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

## 13 个任务族

| 族 | 打什么 | 怎么判 |
|---|---|---|
| `constraint` 约束排班 | 真推理 vs 记题型 | 回溯求解器保证唯一解 |
| `toolchain` 工具链跟随 | 会不会用工具、会不会走神 | 精确数值比对（有不存在的文件、有不可达文件） |
| `cascade` 级联调试 | 会跑代码验证还是靠猜 | **真跑**修复代码 + AST 语义比对 |
| `datatransform` 脏数据转换 | 细节注意力 | 参考实现逐字段比对 |
| `compliance` 硬格式合规 | 听不听话 | 9 条约束逐条机械验证 |
| `multiturn` 需求变更跟随 | 多轮状态跟踪 | 满足 v2 且无 v1 残留 |
| `longcontext` 长文规则应用 | 真读了还是假装读了 | 精确 count/sum |
| `writing` 找错改写 | 判断力 + 表达力 | 代码命中错误点 + **盲评**质量 |
| `repofix` 小仓库调试 | 真 agentic：导航+调试+执行 | 仓库落盘**真跑** + 输出值比对 |
| `longstate` 长程状态维护 | 多轮不丢状态 | 分批下发、逐批精确比对 |
| `premise` 前提辨识 | 用户夹带**错前提**时盲从/纠正/澄清 | 三种情形互为例题，堵死三种套利 |
| `mindiff` 最小 diff | 改一处会不会动十处 | 工作区快照 + 牵动行数预算 |
| `decay` 长会话一致性 | 同一事实问两次会不会漂移 | 真多轮 + 串台引信 + 自报历史核验 |

## 设计公理

1. **能被背下来的题不用** —— 实例运行时程序化生成，答案现场算，seed 可复现
2. **能用代码判的绝不请裁判** —— 只有开放文本才盲评，且必须交换位置双评 + 报告翻转率
3. **harness 不是噪声，是被测量对象** —— 同一模型多套壳子跑，出敏感度指数
4. **测量装置自身要被测** —— 这是做这个项目最大的收获，见上面那 18 条

详见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## 文档地图

| 文件 | 内容 |
|---|---|
| [`docs/DESIGN.md`](docs/DESIGN.md) | 设计思路：四条公理、五层结构、每族埋的坑 |
| [`docs/FINDINGS.md`](docs/FINDINGS.md) | 实测结论（含全部数字与出处） |
| [`docs/MEASUREMENT_DEFECTS.md`](docs/MEASUREMENT_DEFECTS.md) | 18 个测量缺陷清单 + 通用规则 |
| [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md) | 这套设计有效吗？能跟上新模型吗？+ 同类项目对照 |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | 部署、接网关、加任务族、常见坑 |
| `HANDOFF.md` | 项目当前状态与下一步（维护者视角） |
| `CODEX.md` | 自包含的作业指示：给接手的 AI/人（含铁律与踩坑） |

## 目录

```
aipk/
  config.py      网关/名单/预算（用户 yaml 优先，DSH 凭据兜底；密钥不落盘）
  provider.py    OpenAI 兼容适配层：reasoning 归一、工具调用归一、限速、降级重试
  scripted.py    离线满分机器人（demo 用，不需要 key）
  oracle.py      标准答案构造（自检与 demo 共用同一套口径）
  tasks/         13 个任务族：生成器 + 参考实现 + oracle
  harness.py     冻结协议 + 故障注入 + 分阶段 + 仓库落盘 + 工作区快照 + 外部 CLI(Phase B)
  grade.py       位置去偏盲评 + 裁判审计
  metrics.py     Wilson CI / pass^k / 一致性 / 饱和诊断 / 族内子检查 / 裁判审计
  arena.py       Bradley-Terry + bootstrap + harness 敏感度
  report.py      HTML + Markdown + summary.json
  runner.py      编排 + 原始证据落盘
tests/           测量缺陷的回归测试（这个项目的免疫系统）
tools/           裁判标定、多裁判重评、方差报告、harness 对比、探针、排查工具
examples/        示例证据（8 条精简记录，看数据长什么样）
runs/            每轮的原始证据（不进仓库）
```

## 权威性边界

**只支持**「在 seed=X、配置=Y、时间=T 的这次运行中，系统 Z 的表现是……」+ 相对排名与置信区间。
**不支持**「模型 X 整体比 Y 强」，也不给绝对智力分 —— 任务分布是人选的，选择即偏见。

## License

MIT
