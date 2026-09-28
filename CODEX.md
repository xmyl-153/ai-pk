# CODEX.md — 自包含的作业指示（接手者/下一个会话读这份就够）

> 这份文件原本是"交接给 Codex"的作业单；现在按**通用交接文档**维护 ——
> 谁接手（人、Codex、还是下一个 DSH 会话）都读这一份，不需要任何历史对话。
> 最新状态与结论以 [`HANDOFF.md`](HANDOFF.md) 为准，本文负责"怎么干"。

---

## 0. 贴给接手的 AI 的开场白（复制这一段即可）

```
项目在 D:\ai\项目文件夹\ai-pk，Python 3.12，只依赖 httpx + pyyaml（都装好了）。

先做三件事，再动任何代码：
1. 读 D:\ai\项目文件夹\ai-pk\CODEX.md（你的作业指示，全文）
2. 读 D:\ai\项目文件夹\ai-pk\HANDOFF.md 和 README.md、DESIGN.md
3. 跑这两条，确认基线是绿的：
   cd D:\ai\项目文件夹\ai-pk
   python -m aipk selfcheck
   python tests\test_measurement.py

铁律（违反会让整轮数据作废）：
- 跑真模型必须加 --qps 1.2，否则网关 429 限流会把模型打成假 0 分
- 改任务族之后，必须先跑 green 上面两条自检，才能跑真模型
- 别同时跑两个 run（会一起压网关，触发限流）
- 结论里永远不要说「模型 X 整体更强」——只能说「在这次任务分布和配置下」
- 分阶段任务（decay）要放宽 --max-turns（≥40），否则会话被截断
```

---

## 1. 这个项目是什么

**一句话**：用程序化生成的、代码可判定的任务，横向测多个 LLM 的真实质量——因为官方跑分高不等于用起来好。

**它和别的榜单的区别**：防污染生成（没有可背的题库）+ 代码 oracle（不靠 LLM 裁判）+ 冻结工具协议（harness 无关）+ 真实体验指标（延迟/成本/一致性）。

**为什么从零做**：GitHub 上的先例各挖了一半（FastChat 靠人投票、JuryArena 纯裁判、PawBench 静态题库、BeyondBench 只测推理题），**没人把这四件事合起来**。详见 `DESIGN.md` 第 1 节。

---

## 2. 当前状态（2026-09-25 晚）

### 已经能跑、已经跑过

| 项 | 状态 |
|---|---|
| **13 个任务族** | 全部程序化生成 + 代码 oracle，`selfcheck` 全绿 |
| 冻结 harness | 多轮 agent 循环 + 4 个工具（read_file/list_files/run_python/submit）+ 故障注入 + 分阶段任务 + 仓库落盘 + **工作区快照** |
| **Phase B 真实 CLI harness** | **已实现并实测**（`ExternalHarness` + `python -m aipk external`），Codex CLI 跑通 |
| 评分 | Wilson CI / Bradley-Terry + bootstrap / pass^k / 一致性 / 饱和诊断 / 位置去偏盲评 |
| 报告 | HTML + Markdown + summary.json + **族内子检查表**（折进 solved 之前的信号） |
| 回归测试 | 17 项语义比对 + 96 组触发自洽 + 117 实例去重 + premise/mindiff/decay 反套利 |
| 裁判标定 | `tools/judge_calibrate.py`，deepseek-flash 已通过（判别 12/12、位置翻转 0、相同答案 6/6 判平） |
| 累计运行 | **1446 + 270（新族）+ 6（Phase B）= 1722 次**真实调用 |

### 当前有效报告

- 旧 7 族：`runs/20260925-132828/REPORT.md`（630 次）
- **新 3 族：`runs/20260925-201943/REPORT.md`（270 次，10 模型 × premise/mindiff/decay × 3 实例 × 3 重复）**
- harness 对照：`runs/HARNESS_COMPARE.md`

### 已经得出的核心结论（别重复劳动去验证）

**① 正确率维度彻底饱和 —— 连"非确定性维度"也一起饱和了。**
- 旧 7 族：10 模型成功率 90.5%~100%，95% CI 两两重叠
- 新 3 族（前提辨识 / 最小 diff / 长会话一致性）：9/10 模型 100%；
  唯一非满分（seed-2.1-pro 88.9%）的 3 次失败**全是同一个 decay 实例的协议中断**
  （会话只走到第 2~4/9 阶段、最后回复为空且没 submit），**不是答案漂移**
- 漂移率 = 0：走到末次探测的运行里，`首答正确/末答正确/首末一致/自报属实` 四项 100%

→ **推论：凡是"代码可验证的清晰单一目标任务"，前沿模型基本都会做；
　再造更难的同类题是错方向。**

**② 真实区分度全在体验指标 + 族内子检查。**
- 本轮：P90 耗时差 12.2 倍、单任务 token 差 3.8 倍、reasoning 占比差 8 倍、中位首字差 7.7 倍
- 子检查里唯一有信号的：decay 的"中间任务全对"87%（76/87）
  —— 长会话里掉的不是记忆，是中间小任务的执行质量

**③ harness 是变量，而且很贵。**
同一批题、同一模型族（deepseek-flash）、6 组配对：
冻结协议 100% / 7,974 token / 57.1s；Codex CLI 100% / 46,161 token（5.8×）/ 164.6s（2.9×）。
质量打平、成本差近 6 倍。（限制：Codex 走 api.deepseek.com，项目网关虽支持 `/responses`
但拒绝 Codex 必发的 `reasoning.effort` → 差异里含"供给"因素，只能当量级。）

---

## 3. 铁律与踩过的坑（**最重要的一节**）

### 3.1 跑数据的三条硬规则

1. **必须加 `--qps 1.2`**。实测不限速会被网关 429 打成假 0 分：GLM-5.3 一度被误判成 5/24，限速后恢复 4/4。
   代码里 `RunResult.infra_failure` 会把限流/网络故障排除出评分并单独计数——**不要绕过它**。
2. **别同时跑两个 run**。两个进程一起压网关会重新触发限流（本机踩过一次）。
3. **改完任务族先跑 selfcheck + test_measurement，再跑真模型**。这条是用两次作废数据换来的（见 3.2）。

### 3.2 十八个测量缺陷（全部已修，全部写进了 `tests/test_measurement.py`）

**这些坑的共同点：看起来是「某个模型特别差」，查下去全是我的测量代码有问题。**

| # | 缺陷 | 当时的表现 | 修法 |
|---|---|---|---|
| 1 | 不限速被 429 打成假 0 分 | GLM-5.3 被误判 5/24 | 每网关限速 + `infra_failure` 排除 |
| 2 | 触发输入用字符串比对 | cascade 族 24/24 全灭，其实模型全答对 | 改 AST 语义归一 |
| 3 | `reps` 和实例 seed 绑死 | 每个实例只跑一次，pass^k 退化成成功率、一致性恒为 0 | 实例集固定，reps 是对同一实例重复跑 |
| 4 | 跨 profile 比较用 `.get(m, 99)` | 没跑过的模型算出 97 位假漂移 | 只比较跨全部 profile 都有的模型 |
| 5 | 关键字写法落在 `node.keywords` 而非 `node.args` | `items=[...]` 类回答全判错 | 两边都收 |
| 6 | **多参数加圆括号多包一层** | `([7,1,4,1,9],2)` vs `nums=[...],k=2` 差一层 → 10 个模型全被冤枉 | 递归摊平嵌套元组 |
| 7 | **builder 把 `repr(list)` 拼进列表字面量** | 题目自己给出 `nums=[[...]]` 双层嵌套，模型答对反被判错 | 改用 f-string；并加「触发输入自洽性」测试 |
| 8 | 网关流式不报 usage 当 0 | 该模型被算成零成本，成本指标失真 | 用字符数估算并标 `≈` |
| 9 | 裁判 `max_tokens=2048` 被 reasoning 吃光 | writing 族盲评全返回 null | 提到 6144 + 失败重试 + 记无效率 |
| 10 | **隐藏用例用 JSON 字符串比较** | 模型把 `return 0.0` 等价写成 `round(max(amount-paid,0),2)`（返回 int 0）被判"没修对" | 改**数值语义比较**（`same_values`）：0 == 0.0 |
| 11 | **外部 harness 把 `TASK.md`/last-message 写进任务工作区** | 三个完全正确的最小修复被算成"牵动 32 行、3 个文件"→ 全判失败 | 协议文件移出工作区 + `PROTOCOL_ARTIFACTS` 排除名单 |
| 12 | **decay 的"没给出 JSON"提前返回** | 把"会话中途停住"显示成"格式不对"，把真实信号（长会话协议可靠性）埋掉 | 区分「会话没走完」与「格式不对」，理由里给阶段进度 |
| 13 | **HTTP 400 参数不被接受，被算成模型答错** | 给 kimi-k3 传 `temperature=0.7` 被网关直接 400 → 那一轮 **18/18 全灭、pass^k 掉到 0%** | `classify_http_error()`：参数类 400 一律不算模型失败 + 自动降级重投 + 报告标注「参数降级」 |
| 14 | **裁判模型名写错 → 盲评静默消失** | `_split_key` 只认 `provider/model`，写裸模型名会抛异常并被吞成"裁判不可用"，整轮盲评凭空没有 | 支持裸模型名解析 + 解析失败**大声告警**，不再静默继续 |
| 15 | **"裁判不评自己"的判据没归一化** | 传 `deepseek-flash`（裸名）而运行记录 key 是 `jiyuanapi/deepseek-flash`，两者不相等 → 排除静默失效，**裁判给自己的答案打了分**，那一格作废 | 排除判据改用归一化 key，并加回归测试锁死这条不变量 |
| 16 | **裁判"没判出来"被当成平局** | `Judge.compare` 一次有效裁决都没有时 winner 保持默认 `"tie"`，runner 折算成 **0.5 分** → 裁判罢工反而白送模型半分 | `JudgeVerdict.invalid` 显式标无效；runner 不写分数只记 `judge_invalid`；审计把它算进无效率 |
| 17 | **多裁判重评时判决键少了模型维度** | 键写成 `(题, rep, 裁判)` → 同一格 10 个模型共用一条判决：**分数全一样、Kendall τ 假模假样 =1.000**，`--reuse` 还把 A 模型的判决当成 B 模型的（270 次里 261 次"复用"） | 键加 `model_key`，并加回归测试；这类"收敛得漂亮"的结果比崩掉更危险 |
| 18 | **饱和诊断的"族数"判据会误判整体饱和** | 式子 `len(sat_fams) >= max(1, len(sat)+len(disc)-3)` 在"5 族里 2 族饱和、3 族仍有区分度、极差 25pp"时也判**整体饱和** —— 而这是报告标题级结论 | 改成两条明确理由（极差 <15pp / 没有任何族能分开模型），并把理由写进报告；加回归测试 |

### 3.3 参数与网关兼容性（实测）

| 现象 | 说明 |
|---|---|
| `temperature=0.7` 给 **kimi-k3** | 阿里云 MaaS 直接 400：`Parameter 'temperature'=0.7 is not supported`。已自动降级（不传该参数）并在报告里标「参数降级」—— **它的稳定性指标不能与别人直接比** |
| `temperature=0` 是否确定？ | **不是**。实测自由生成任务（起小名）qwen3.8-flash 在 `temperature=0` 下 6 次调用出 3 种答案。所以"温度=0 ⇒ 可复现"是错的；本项目的方差结论都建立在**同题多次重复**上，而不是"一次可复现" |
| Codex CLI 0.155 | 已移除 `wire_api="chat"`，只认 `responses`；项目网关 `tokenrhythm.studio` 支持 `/responses`，但拒绝 Codex 必发的 `reasoning.effort`（`UNKNOWN_FIELD`）→ 无法同网关对照 |

### 3.4 环境坑

- **PowerShell 5.1 读 UTF-8 JSON 会乱码** → 一律用 python 读，别用 `ConvertFrom-Json`
- **含中文的文件只用编辑工具改**，不要用 pwsh 做字符串替换（PS5.1 按 GBK 解码会把整份中文写坏）
- 日志重定向用 `*>&1 | Out-File -Encoding utf8`（只重定向 stdout，遇 stderr 会中断作业）
- **`github.com` 在本机被 hosts 指向 127.0.0.1**（Steam++ 加速），`git clone` / `pip install` 从 GitHub 取包可能失败，需要走镜像
- 密钥在 `C:\Users\20684\.dsh\.credentials.yaml`，通过 `settings.yaml` 的 `llm-pi-ai.providers` 映射到三个网关。**不要把密钥写进任何被提交的文件**

---

## 4. 你的作业（按优先级，从最容易见效的开始）

### 任务进度板（2026-09-25 晚）

| 任务 | 状态 |
|---|---|
| A-2 错误前提辨识 | ✅ 完成（`f11_premise.py`，90/90 饱和） |
| A-4 最小 diff | ✅ 完成（`f12_mindiff.py`，89/89 饱和） |
| A-1 长会话一致性 | ✅ 完成（`f13_decay.py`，漂移率 0，只有协议中断信号） |
| **A-3 输出方差** | ✅ **完成**：`--temperature 0.7` 重跑 180 次 → **正确性翻转 0%、实质漂移 0%**；但自由生成任务在 `temperature=0` 下就已经漂（见 3.3）→ **方差是任务类型的属性，不是采样参数的属性**。工具 `tools/variance_report.py`、`tools/probe_temperature.py` |
| B Phase B 真实 harness | 🟡 已实现并实测（Codex CLI、对照报告已出）；**待扩样本** |
| **C 裁判升级** | ✅ **完成**：3 个候选单独标定全部通过（判别 12/12、翻转 0、相同答案 6/6 判平），择优取 `deepseek-flash`（准确率相同、判决快 3.7 倍）；`writing` 族已打开盲评；`judge_audit()` 终于接进报告（位置翻转率/无效率/判平率） |
| E 盲评硬化 | 🟡 工具已就绪（`tools/rejudge.py`：多裁判委员会 + 裁判间 τ + 分数区间），实跑数据见 `runs/REJUDGE.md` |
| D 规模化（≥10 实例/族） | ⬜ 未做 |

### 任务 A-3：输出方差（已完成，结论留住）

```bash
python -m aipk run --seed 2026 --reps 3 --tasks-per-family 3 --max-turns 20 \
  --temperature 0.7 --qps 1.2 --workers 6 --no-judge --families premise,mindiff
python tools\variance_report.py runs\<temp0> runs\<temp0.7>   # 对照：翻转率/实质漂移/原样漂移
python tools\probe_temperature.py                             # 先验：网关到底听不听 temperature
```

**结论（别重复劳动）**：180 次运行里 **正确性翻转 0%、实质漂移 0%**（10 模型全部 pass^k 100%）。
但**开放式任务相反**：`writing` 族 90 次运行里**实质漂移 100%**（同一题 3 次改写 3 次都不一样）。
而自由生成任务在 `temperature=0` 下就已经不确定（探针实测 6 次出 3 种答案）。
→ **要测方差，得用开放式任务；用代码可验证的题测方差是测了个空。**
详见 `runs/VARIANCE-STRUCTURED.md` 与 `runs/VARIANCE-WRITING.md`。

### 任务 C：裁判升级（已完成）

```bash
python tools\judge_calibrate.py deepseek-flash glm-5.3 kimi-k3   # 每个候选单独标定 + 择优
python -m aipk run --families writing --reps 3 --tasks-per-family 3 \
  --judge deepseek-flash --qps 1.2 --workers 6                   # 打开盲评（不要 --no-judge）
python -m aipk report --run runs\<这次>                           # 报告里会有「裁判审计」段
```
- 三个候选标定**全部通过**（判别 12/12、翻转 0、相同答案 6/6 判平），择优取 `deepseek-flash`
  （准确率相同、单次判决中位耗时 6.25s，比 glm-5.3 的 23.16s / kimi-k3 的 27.67s 快 3~4 倍）。
- **实战翻转率远高于标定**：writing 上 90 次判决的位置翻转率 **24.4%**（标定题里是 0%）。
  原因：实战两份答案常常"都好"，裁判拿不定主意。→ 盲评分只能当弱证据，
  要硬化请上多裁判委员会（`Judge` 支持多 provider）或把判决数提到 ≥30。
- 裁判自己的答案**不参与盲评**（防自偏好）：选谁当裁判，谁的格子就空着，
  要用另一个已标定裁判补跑那一格（本轮就是这么补的 deepseek-flash）。

### 任务 B：把 Phase B 扩到同规模

接口已经能用，`ExternalHarness` 的三份协议（workspace 铺设 / 执行超时 / 产物读取）
都在 `aipk/harness.py` 里写死了，照抄即可：

```bash
python -m aipk external --families premise,mindiff --tasks-per-family 3 --reps 3 \
  --env-cred DEEPSEEK_API_KEY --model-key deepseek/deepseek-flash --timeout 600 --workers 2
python tools\harness_compare.py runs\<冻结> runs\<外部> ^
  --map "deepseek/deepseek-flash=jiyuanapi/deepseek-flash"
```
**成本警告**：外部 CLI 单题约 46k token（冻结 harness 约 8k），扩样本前先估预算。
想接 Claude Code / 别的 CLI，只要把 `ExternalCLI.cmd` 换掉、并确认它的
"最后一条消息"能落到文件（`-o`）或工作区 `ANSWER.json`。

### 任务 C：裁判升级

1. 标定 3 个以上候选裁判（`python tools\judge_calibrate.py <模型名>`），择优
2. 把标定结果附进报告
3. 给 `writing` 族打开盲评（去掉 `--no-judge`），把 `judge_audit()` 的翻转率写进报告

### 任务 E：盲评硬化（进行中 —— 本轮暴露出的新任务）

writing 上单裁判的位置翻转率 **24.4%**（标定题里是 0%），所以那份排名是弱证据。
硬化手段是**多裁判委员会 + 加大判决数**，工具已经写好：

```bash
python tools\rejudge.py runs\WRITING-JUDGED \
  --judges deepseek-flash,glm-5.3,kimi-k3 --reuse --workers 3 --out runs\REJUDGE.md
```
`rejudge.py` 直接用**已落盘的答案**重跑裁判（不重跑任务，省一大笔），输出：
单裁判实战翻转率、委员会判平率、**裁判之间的 Kendall τ**（τ 低 = 名次本身是噪声）、
每个模型的分数区间（宽度 ≥0.5 的换裁判就改结论）。

注意成本：glm-5.3 / kimi-k3 单次判决中位耗时 23s / 28s，90 份答案 × 3 裁判 ≈ 40 分钟。

### 任务 D：规模化

```bash
python -m aipk run --seed 2026 --reps 3 --tasks-per-family 10 --max-turns 20 \
  --qps 1.2 --workers 6
```
注意：10 模型 × 7 族 × 10 实例 × 3 重复 = 2100 次运行，按当前吞吐约 3.5 小时。

---

## 5. 常用命令

```bash
cd D:\ai\项目文件夹\ai-pk

# 自检（改完代码必跑）
python -m aipk selfcheck              # 任务族 oracle 自洽 + 撞题检查
python tests\test_measurement.py      # 防测量错误的回归测试

# 看有什么
python -m aipk list                   # 网关 + 首战名单 + 已知不可用模型
python -m aipk preview --family repofix --seed 2026   # 预览一道题长什么样（调题目必用）

# 单个冒烟
python -m aipk smoke --model glm-5.3 --family repofix

# 正式 PK
python -m aipk run --seed 2026 --reps 3 --tasks-per-family 3 --max-turns 20 \
  --qps 1.2 --workers 6 --no-judge \
  --families repofix,longstate,toolchain,cascade,writing,datatransform,longcontext

# 新 3 族（注意 decay 要多给轮数，否则会话会被截断）
python -m aipk run --seed 2026 --reps 3 --tasks-per-family 3 --max-turns 45 \
  --qps 1.2 --workers 6 --no-judge --families premise,mindiff,decay

# Phase B：真实 CLI 当壳子
python -m aipk external --families premise,mindiff --tasks-per-family 3 --reps 1 \
  --env-cred DEEPSEEK_API_KEY --model-key deepseek/deepseek-flash --timeout 600 --workers 2

# 出报告 / 复算
python -m aipk report --run runs\20260925-201943
python tools\harness_compare.py runs\<冻结> runs\<外部> --map "A=B"
python tools\merge_report.py          # 多个 run 合并（改开头的 RUNS 列表）

# 裁判标定
python tools\judge_calibrate.py deepseek-flash
```

---

## 6. 代码地图

```
aipk/
  config.py      网关/模型名单/运行预算。密钥从 DSH credentials 读，不落盘
  provider.py    OpenAI 兼容适配层：reasoning 字段归一、工具调用归一、
                 按网关限速、429 尊重 retryAfterSeconds、usage 缺失时估算
  tasks/         13 个任务族，一族一文件，@register 自动注册
  harness.py     冻结工具协议 + 多轮循环 + 故障注入 + 分阶段任务 + 仓库落盘
                 + 工作区快照（判"改了几行"用）
                 + ExternalHarness（Phase B：真实 CLI 当壳子，已可用）
  grade.py       位置去偏盲评（交换位置评两遍）+ 裁判审计 + 配对赛程
  metrics.py     Wilson CI / pass^k / 一致性 / 综合分 / 权重敏感性 / 饱和诊断
                 / 族内子检查（check_breakdown）
  arena.py       Bradley-Terry + bootstrap CI + harness 敏感度（Kendall τ）
  report.py      HTML + Markdown + summary.json（含族内子检查表）
  runner.py      编排 + 原始证据落盘（每轮的 transcript/工具调用/用量/错误）
tests/
  test_measurement.py   防测量错误的回归测试（这个项目的免疫系统）
tools/
  judge_calibrate.py    裁判标定（用已知答案测裁判准不准）
  harness_compare.py    冻结协议 vs 外部 CLI 的同题对比
  probe_responses_api.py 网关能力探针（例如是否支持 Responses API）
  merge_report.py       多 run 合并成一份报告
runs/                   每次运行的原始证据（runs.jsonl）
```

**`runs/*/runs.jsonl` 是唯一的真相来源**。报告全部可以从它复算出来——如果对某个数字有疑问，直接读原始记录，不要猜。

---

## 7. 这个项目的权威性边界（写报告时必须遵守）

只支持这样的结论：
- ✅「在 seed=X、配置=Y、时间=T 的这次运行中，系统 Z 的表现是……」
- ✅ 相对排名 + 置信区间 + 可复现的失败证据

不支持：
- ❌「模型 X 整体比 Y 强」—— 任务分布是人选的，选择即偏见
- ❌ 绝对智力分

四条对策已经做进代码，不要削弱它们：
1. 原始证据全落盘，任何人可复算
2. 权重敏感性分析（换权重后排名翻转就明说「这几位差不多」）
3. `pass^k` 与一致性，不报均值孤点
4. 裁判可审计（位置翻转率、无效率），且**先过标定再进报告**

---

## 8. 一句话总结现状

**引擎是好的、证据链是完整的、结论已经出来了：正确率维度（含"非确定性维度"）全面饱和，
体验指标与族内子检查才是区分度，harness 换壳子能差近 6 倍成本。**
**下一步的价值不在于"造更难的代码题"，而在于：输出方差（temperature>0）、
把 Phase B 扩到同规模、裁判升级、样本规模化。**
