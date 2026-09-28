# AI PK 项目 · 续接说明（2026-09-28 开源封装 更新）

> 这份文件是**维护者视角**的当前状态与下一步；对外文档在 `README.md` 与 `docs/`：
> `README.md`（门面）· `docs/DESIGN.md`（设计思路）· `docs/FINDINGS.md`（实测结论）·
> `docs/MEASUREMENT_DEFECTS.md`（18 个缺陷）· `docs/ASSESSMENT.md`（有效性评估 + 同类项目对照）·
> `docs/DEPLOY.md`（部署）· `CONTRIBUTING.md`（怎么贡献）

## 一句话现状

**13 个任务族、900+ 次真模型运行、Phase B 跑通、裁判标定 + 多裁判硬化、
已封装成可开源仓库（首次提交 `187d621`：51 个文件 / 456 KB，无密钥、无 runs）。**
代码在 `D:\ai\项目文件夹\ai-pk`。

## 开源封装做了什么（2026-09-28）

| 项 | 内容 |
|---|---|
| **离线可跑** | `aipk/scripted.py`（满分/错答机器人）+ `python -m aipk demo`：**不需要 API key** 跑通全链路（13 族 → harness → 工具落盘 → 判定 → 报告）。既是门面，也是端到端回归测试（满分机器人必须 100%、错答必须 0%） |
| **不绑定作者的凭据系统** | `aipk/config.py` 支持 `aipk.config.yaml`（providers + roster，任何 OpenAI 兼容端点），DSH 凭据只作兜底；`python -m aipk init` 生成模板 |
| **口径集中** | 标准答案构造从 `__main__` 提到 `aipk/oracle.py`，自检与 demo 共用，避免两边漂移（缺陷 #7 就是口径不一致） |
| **仓库卫生** | `LICENSE`(MIT) · `.gitignore`（排除 runs/、密钥、本地配置）· `.gitattributes`（统一 LF）· `pyproject.toml` · `requirements.txt` · `examples/sample_run.jsonl`（8 条精简证据） |
| **开源前自检** | `tools/secret_scan.py` 扫密钥/绝对路径/邮箱（已反向验证：塞一个假 key 能被抓到） |
| **对外文档** | `docs/` 五篇 + `CONTRIBUTING.md`（写清四类最有价值的贡献） |

## 已完成的实验

| 运行 | 内容 | 结果 |
|---|---|---|
| `runs/20260923-203751` | 10 模型 × 8 族 × 3 重复 × 3 实例 = 720 次 | 第一轮基线（cascade grader 有 bug，数据已作废） |
| `runs/COMBINED` | 816 条（含 harness 敏感度实验） | 第一轮报告 |
| `runs/20260925-132828` | 10 模型 × 7 族 × 3 × 3 = 630 次，0 故障 | 旧 7 族的有效报告 |
| `runs/20260925-201943` | 10 模型 × 3 个新族（premise/mindiff/decay）× 3 × 3 = 270 次 | 新族有效报告 |
| `runs/20260927-155054` + `runs/TEMP07-COMBINED` | 同 2 族 **`--temperature 0.7`** × 180 次 | 方差实验：**翻转 0%、实质漂移 0%** |
| `runs/WRITING-JUDGED` | `writing` 族带**盲评** 90 次（裁判 deepseek-flash） | 盲评相对分 0.00~0.94；**位置翻转率 24.4%** |
| `runs/20260927-184720` | 补跑 deepseek-flash 的 writing 格（裁判换成 glm-5.3） | 修掉"裁判自己评自己" |
| `runs/20260925-213123` | Codex CLI × 2 族 × 3 实例 = 6 次（Phase B） | 外部 harness 对照 |
| `runs/HARNESS_COMPARE.md` | 冻结协议 vs Codex CLI 同题对比 | 6/6 质量打平、成本差 5.8 倍 |
| `runs/judge_calibration.json` | 3 个候选裁判单独标定 + 择优 | 三个全部三项全过；选 `deepseek-flash` |
| `runs/VARIANCE-STRUCTURED.md` / `VARIANCE-WRITING.md` | 输出方差报告（结构化任务 vs 开放式任务） | 0% vs **100%** |

## 本轮（2026-09-27）新增的三件事

### 1. 任务 A-3 输出方差：结论是「方差是任务类型的属性」

两条证据（都是本轮实测，脚本可复跑）：

| 任务类型 | 配置 | 正确性翻转 | **实质漂移** |
|---|---|---|---|
| 结构化可验证（premise / mindiff） | `temperature=0.7`，180 次 | 0% | **0%** |
| 结构化可验证（同上） | `temperature=0`，270 次 | 0% | **0%** |
| **开放式改写（writing）** | `temperature=0`，90 次 | 0% | **100%** |

- 结构化任务：**同一题重复跑总是收敛到同一个答案**（连措辞之外的实质内容都一致），
  把温度从 0 升到 0.7 也测不出漂移（10 个模型 `pass^k` 全 100%）。
- 开放式任务：**同一题 3 次改写，3 次都不一样**（实质漂移 100%，全部 10 个模型）。
- 先验探针（`tools/probe_temperature.py`）还发现：**`temperature=0` 并不确定** ——
  自由生成任务（起小名）qwen3.8-flash 6 次调用出 3 种答案。
- → **想测"抽卡感"，必须用开放式任务；用代码可验证的题测方差是测了个空。**
- 工具：`tools/variance_report.py`（正确性翻转 / 实质漂移 / 原样漂移，两次运行对照）。

### 2. 任务 C 裁判升级：标定 + 择优 + 接进报告

- `tools/judge_calibrate.py` 改成**每个候选单独标定**（早期把多个候选塞进一个 Judge，
  测的是"委员会"看不出个体差异），择优规则明确写死：
  三项全过 → 无效裁决率 → 重试次数 → 判决耗时。
- 实测三个候选**全部通过**：deepseek-flash / glm-5.3 / kimi-k3 都是
  判别 12/12、位置翻转 0、相同答案 6/6 判平；差别只在速度
  （单次判决中位耗时 6.25s / 23.16s / 27.67s）→ **择优取 `deepseek-flash`**。
- `judge_audit()` 写好了却从没被报告调用过 —— 本轮接上，
  报告新增「裁判审计」段：`runs/WRITING-JUDGED/REPORT.md` 显示
  判决 90 次、无效 0、**位置翻转率 24.4%**、判平率 25.6%。
- **重要发现：标定通过 ≠ 实战无偏。** 标定用的 6 组已知好坏里翻转率是 0，
  但在 writing 的实战对比里（模型改写 vs 参考改写，常常是"都好"）翻转率飙到 24.4%。
  → 盲评相对分**有区分度**（0.00~0.94，而正确率是全员 100%）但**只能当弱证据**；
  要硬化得上多裁判委员会或加大判决数。
- 顺带修掉一个静默失败：裁判模型名写裸名（`deepseek-flash`）会被解析异常吞成
  "裁判不可用"，整轮盲评凭空消失 → 现在支持裸名解析 + 失败大声告警。

### 3. 又修掉六个测量缺陷（#13 ~ #18）

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| 13 | **HTTP 400 参数不被接受被算成模型答错** | `temperature=0.7` 给 kimi-k3 被网关 400 拒绝 → 18/18 全灭、pass^k 掉到 **0%** | `classify_http_error()`：参数类 400 不算模型失败 + 自动降级重投 + 报告标注「参数降级」 |
| 14 | **裁判模型名写错 → 盲评静默消失** | `_split_key` 只认 `provider/model`，写裸模型名抛异常被吞成"裁判不可用" | 支持裸模型名 + 解析失败大声告警 |
| 15 | **"裁判不评自己"判据没归一化** | 裸名与运行记录 key 不相等 → 排除静默失效，**裁判给自己的答案打了分** | 排除判据改用归一化 key（回归测试锁死） |
| 16 | **裁判"没判出来"被当成平局** | 一次有效裁决都没有时 winner 默认 `tie` → runner 折算 **0.5 分**，裁判罢工反而白送半分 | `JudgeVerdict.invalid` 显式标无效；不写分数、只记 `judge_invalid`；审计算进无效率 |
| 17 | **多裁判重评的判决键少了模型维度** | 键 `(题, rep, 裁判)` → 同格 10 个模型共用一条判决：**分数全一样、Kendall τ 假模假样 =1.000**，`--reuse` 张冠李戴 261/270 | 键加 `model_key`（回归测试锁死）；这类"收敛得漂亮"的结果比崩掉更危险 |
| 18 | **饱和诊断的"族数"判据会误判整体饱和** | "5 族里 2 族饱和、3 族仍有区分度、极差 25pp"也被判**整体饱和** —— 而这是报告标题级结论 | 改成两条明确理由（极差 <15pp / 没有任何族能分开模型）并写进报告；加回归测试 |

**教训依旧是同一句**：每一次"某个模型表现异常差"或"某个指标好得不真实"，
查下去都是我的测量代码有问题。本轮 13~18 六条都是如此。

### 4. 盲评硬化（本轮新增任务 E）

上一轮暴露出：标定题里三个裁判翻转率都是 0，但 `writing` 实战里单裁判翻转率 **24.4%**。
本轮做了两件事：

- **报告会按裁判拆分**：`judge_audit()` 新增 `per_judge`，报告「裁判审计」段在多裁判时
  列出每个裁判的判决数/翻转率/判平率（看清是谁在翻）。
  实测 `WRITING-JUDGED`：deepseek-flash 81 次判决翻转 **27.2%**，glm-5.3 那 9 次翻转 0%。
- **`tools/rejudge.py`（新）**：拿**已落盘的答案**重跑多裁判委员会，
  不重跑任务（省一大笔），并输出四件事：单裁判实战翻转率、委员会判平率、
  **裁判之间的 Kendall τ**（τ 低 = 名次本身是噪声）、每个模型的分数区间（跨裁判极差）。
  判决会落盘成 `.cache.json`（判决很贵，必须当证据留下，重分析不再花钱）。
  结果见 `runs/REJUDGE.md`。

## 本轮新增的三个任务族（打的是"非确定性维度"）

| 族 | 打什么 | 怎么判 | 结果 |
|---|---|---|---|
| `premise` 前提辨识 | 用户在请求里夹一句**错前提**，模型是盲从、纠正还是澄清 | 三种情形（矛盾/一致/规范未定义）互为例题，全部机械可判 | **90/90 = 100%，饱和** |
| `mindiff` 最小 diff | 改一处会不会动十处（顺手重构/格式化/改自测） | harness 抓**工作目录最终快照**，与原始仓库比"牵动行数"，预算 = 最小改动 +3 行 | **89/89 = 100%，饱和** |
| `decay` 长会话一致性 | 同一事实在长会话里被问两次，答案会不会漂移；自报历史是否属实 | 真多轮（8~10 阶段，中间夹"另一个项目同名字段"的串台引信），逐项判 | **87 次里 84 次全对**，唯一失败见下 |

### 本轮的核心结论

1. **三个"非确定性维度"也基本饱和了。** 10 个模型 9 个 100%，
   唯一非满分的 seed-2.1-pro（88.9%）3 次失败**全是同一个 decay 实例、同一原因**：
   会话只走到第 2~4/9 阶段就停了、最后一条回复为空且没调 `submit`
   —— 这是**长会话里的协议可靠性**问题，**不是**答案漂移。
2. **漂移率本身是 0**：所有走到末次探测的运行，`首答正确`/`末答正确`/`首末一致`/`自报属实`
   四项全是 100%。也就是说"~10 阶段、~2 万 token 的会话"对前沿模型不构成一致性压力。
3. **真正有信号的是子检查**：`decay` 的"中间任务全对"只有 **87%（76/87）**——
   长会话里掉的不是记忆，是**中间小任务的执行质量**。报告新增的
   「族内子检查」表就是为这类信号准备的（成功率会把它折成一个布尔值丢掉）。
4. **体验指标依旧是唯一稳定的区分度**（本轮 27 次/模型）：
   - P90 耗时 57.5s（qwen3.8-flash）~ 702.2s（seed-2.1-pro）= **12.2 倍**
   - 单任务 token 5088（kimi-k3）~ 19326（seed-2.1-pro）= **3.8 倍**
   - reasoning 占比 12.2%（deepseek-flash）~ 98.4%（kimi-k3）= **8 倍**
   - 中位首字 595ms ~ 4.6s = **7.7 倍**

## Phase B：真实 CLI 当壳子（本轮打通）

`aipk/harness.py` 的 `ExternalHarness` **不再抛 NotImplementedError**，已实测可用：

- **workspace 协议**：每任务一个临时工作目录，题目文件全部落盘；
  harness 自己的协议文件（`TASK.md`、last-message）写在**工作区之外**
- **执行**：`codex exec --cd <ws> --skip-git-repo-check --sandbox workspace-write
  -c approval_policy=never --json -o <file> -`（prompt 走 stdin，避开 Windows 转义）
- **产物读取**：last-message 文件为主，工作区 `ANSWER.json` 兜底；
  **工作区快照**一并交给同一个 oracle（所以"到底改没改文件"照样成立）
- **用量/步数**：从 CLI 的 JSONL 事件流回收（回收不到就按字符数估算并标 ≈）

### 命令

```bash
python -m aipk external --families premise,mindiff --tasks-per-family 3 --reps 1 \
  --env-cred DEEPSEEK_API_KEY --model-key deepseek/deepseek-flash --timeout 600 --workers 2
python tools\harness_compare.py runs\20260925-201943 runs\<外部那次> ^
  --map "deepseek/deepseek-flash=jiyuanapi/deepseek-flash" --label-b "Codex CLI"
```

### 实测结论（同题 6 组配对）

| 壳子 | 成功率 | 内部步骤 | 平均 token | 平均耗时 |
|---|---|---|---|---|
| 冻结协议 frozen-v1 | 100% | 6.0 | **7,974** | **57.1s** |
| Codex CLI | 100% | 4.5 | **46,161（5.8×）** | **164.6s（2.9×）** |

**质量打平、成本差近 6 倍** —— 这就是"harness 是被测量对象"的第一份本机证据。

**必须写明的限制**：Codex 走它自己配置的 `api.deepseek.com`，不是本项目的网关
（本项目网关 `tokenrhythm.studio` 实测**支持 `/responses`**，但会拒绝 Codex 必发的
`reasoning.effort` 字段，报 `UNKNOWN_FIELD` → 无法做"同网关同模型"的严格对照）。
所以上表的差异里**同时含"壳子"和"供给"两个因素**，只能当量级，不能当精确归因。

## 本轮修掉的三个测量缺陷（都补了回归测试）

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| 10 | 隐藏用例用 JSON **字符串**比较 | 模型把 `return 0.0` 等价改成 `round(max(amount-paid,0),2)`（返回 int 0）被冤枉成"没修对" | 改成**数值语义比较**（`same_values`），0 == 0.0 |
| 11 | 外部 harness 把 `TASK.md`/`_last_message.txt` 写进任务工作区 | 三个**完全正确**的最小修复被算成"牵动 32 行、3 个文件"→ 全判失败 | 协议文件移出工作区 + `PROTOCOL_ARTIFACTS` 排除名单 |
| 12 | decay 的"没给出 JSON"提前返回 | 把"会话中途停住"显示成"格式不对"，真实信号被埋 | 区分「会话没走完」与「格式不对」，理由里给出阶段进度 |

**教训依旧是同一句**：每一次"某个模型表现异常差"，查下去都是我的测量代码有问题。
本轮三次全部如此（#10 冤枉 3 次、#11 冤枉 3 次、#12 误报 3 次）。

## 未完成 / 下一步（按价值排序）

0. **发布到 GitHub（只差网络）**：仓库已初始化并完成首次提交 `187d621`。
   本机实测 **github.com 不可达**（`git ls-remote` 报 `Connection was reset`；
   系统代理关闭、无本地代理端口在听）→ 需要先开加速器（Steam++ 之类）或换网络，然后：

   ```bash
   # 先在 GitHub 上建一个空仓库（不要勾 README/LICENSE），然后：
   git remote add origin git@github.com:xmyl-153/ai-pk.git   # 或 https://...
   git branch -M main
   git push -u origin main
   ```
   推送前再跑一次 `python tools/secret_scan.py` 与 `git status`（确认 `runs/`、`aipk.config.yaml` 没被跟踪）。
   没有 gh CLI，建仓库这一步要在网页上做，或 `winget install GitHub.cli` 后 `gh repo create ai-pk --public --source=. --push`。

1. **盲评硬化落地**（`runs/REJUDGE.md`）：已实测裁判之间平均 Kendall τ = 0.467、
   实战翻转率 26%，且 glm-5.3/kimi-k3 在长提示下单次判决要几分钟（成本极高）。
   → 结论方向：**别加裁判**（*Nine Judges, Two Effective Votes* 证明加裁判收益极低），
   改用**少量人类标注锚点**校准裁判，或换成可验证的代理指标。
2. **把 Phase B 扩到同规模**（当前只有 6 组配对）。外部 CLI 单题约 46k token（约冻结 harness 的 6 倍）。
3. **任务 D：规模化** —— 现在 3 实例/族（27 次/模型），要报更硬的结论需要 ≥10 实例/族。
4. **继续找有区分度的维度**：正确率三连饱和 → 往"成本/行为/开放式质量"走，
   但开放式质量先解决裁判噪声（第 1 条）。

## 机制提醒（踩过就别再踩）

- **必须加 `--qps 1.2`**：不限速会被网关 429 打成假 0 分
- **别同时跑两个 run**（会一起压网关）——例外：外部 CLI 走别的端点时可以并行
- **改完任务族先跑** `python -m aipk selfcheck` + `python tests\test_measurement.py`
- PowerShell 5.1 读 UTF-8 JSON 会乱码 → 用 python 读
- 含中文的文件只用编辑工具改，不要用 pwsh 做字符串替换
- 日志重定向用 `*>&1 | Out-File -Encoding utf8`
- `--max-turns` 要按族设：`decay` 需要 40+，否则会话被截断（grader 会明说"被截断"）

## 文件地图

```
aipk/tasks/      13 个任务族（f01..f13，一族一文件，@register 自动注册）
aipk/harness.py  冻结协议 + 故障注入 + 分阶段任务 + 仓库落盘 + 工作区快照
                 + ExternalHarness（Phase B，已可用）
aipk/grade.py    位置去偏盲评 + 裁判审计
aipk/metrics.py  Wilson CI / pass^k / 一致性 / 综合分 / 饱和诊断 / 族内子检查
aipk/arena.py    Bradley-Terry + bootstrap + harness 敏感度
aipk/report.py   HTML + MD + summary.json（含「族内子检查」表）
tests/           回归测试（防测量错误，这个项目的免疫系统）
tools/           judge_calibrate.py 裁判标定（每个候选单独考，再择优）
                 rejudge.py         多裁判委员会重评已落盘答案（判决落盘成 cache）
                 harness_compare.py 冻结 vs 外部 CLI 对比
                 variance_report.py 同题重复跑的漂移（翻转/实质漂移/原样漂移）
                 inspect_run.py      排查"某个模型异常差"的第一现场
                 probe_temperature.py 网关到底听不听 temperature
                 probe_responses_api.py 网关能力探针
                 merge_report.py 多 run 合并
runs/            每次运行的原始证据（runs.jsonl 是唯一真相来源）
```
