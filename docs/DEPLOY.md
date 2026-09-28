# 部署与使用

## 0. 30 秒体验（不需要任何 API key）

```bash
git clone https://github.com/xmyl-153/ai-pk && cd ai-pk
pip install -r requirements.txt          # 只有 httpx + pyyaml

python -m aipk selfcheck                 # 13 个族的 oracle 自洽（"对的不能被判错"）
python -m aipk demo                      # 离线跑通全链路：满分机器人 100%、错答机器人 0%
python tests/test_measurement.py         # 18 个测量缺陷的回归测试
```

`demo` 会真的走完 任务生成 → 多轮 harness → 工具调用（含真改文件）→ 判定 → 报告，
全程本地、零调用、零花费。**先确认装置是好的，再花钱。**

## 1. 接自己的网关

```bash
python -m aipk init                      # 生成 aipk.config.yaml 模板
```

```yaml
providers:
  mygateway:
    base_url: https://api.example.com/v1   # 任何 OpenAI 兼容端点
    api_key_env: MY_GATEWAY_API_KEY        # 密钥只放环境变量
roster:
  - [mygateway, strong-model, "某旗舰", flagship]
  - [mygateway, fast-model,   "某轻量", light]
run:
  qps_per_gateway: 1.2                     # **别去掉限速**：不限速会被 429 打成假 0 分
```

```bash
export MY_GATEWAY_API_KEY=sk-...          # Windows: $env:MY_GATEWAY_API_KEY="sk-..."
python -m aipk list                       # 确认读到了
python -m aipk smoke --model strong-model --family premise   # 单模型冒烟
```

没配 `aipk.config.yaml` 时，程序会回退到 DSH 的 `~/.dsh/settings.yaml` +
`.credentials.yaml`（作者本机用法，别人的机器上通常没有）。

## 2. 跑一轮正式 PK

```bash
# 13 族全跑：10 模型 × 13 族 × 3 实例 × 3 重复 = 1170 次（按吞吐约 1~2 小时）
python -m aipk run --seed 2026 --reps 3 --tasks-per-family 3 \
  --max-turns 20 --qps 1.2 --workers 6 --no-judge

# 只跑新 3 族（注意 decay 要放宽轮数，否则会话被截断）
python -m aipk run --reps 3 --tasks-per-family 3 --max-turns 45 \
  --qps 1.2 --workers 6 --no-judge --families premise,mindiff,decay

# 带盲评（开放式任务才有意义；裁判必须先过标定）
python tools/judge_calibrate.py deepseek-flash glm-5.3
python -m aipk run --families writing --reps 3 --tasks-per-family 3 --judge deepseek-flash
```

**三条硬规则**（违反会让整轮数据作废）：

1. 必须 `--qps 1.2`，否则会被网关 429 打成假 0 分
2. 改完任务族先跑 `selfcheck` + `test_measurement.py`，再跑真模型
3. 别同时跑两个 run（会一起压网关）；分阶段族要给足 `--max-turns`

## 3. 出报告与复算

```bash
python -m aipk report --run runs/<时间戳>      # REPORT.md + report.html + summary.json
python -m aipk preview --family premise        # 看看题目长什么样
python tools/inspect_run.py runs/<时间戳>/runs.jsonl --errors   # 排查"某个模型异常差"
```

报告里有什么、怎么读：见 [`DESIGN.md`](DESIGN.md) 与 [`FINDINGS.md`](FINDINGS.md)。
**`runs/<时间戳>/runs.jsonl` 是唯一真相来源** —— 对任何数字有疑问就读原始记录，别猜。

## 4. 进阶：把真实 CLI 当壳子（Phase B）

```bash
python -m aipk external --families premise,mindiff --tasks-per-family 3 --reps 1 \
  --env-cred DEEPSEEK_API_KEY --model-key deepseek/deepseek-flash --timeout 600 --workers 2

python tools/harness_compare.py runs/<冻结那次> runs/<外部那次> \
  --map "deepseek/deepseek-flash=jiyuanapi/deepseek-flash" --label-b "Codex CLI"
```

- 外部 CLI 需要"最后一条消息能落到文件"（`-o`）或工作区里写 `ANSWER.json`
- 换 Claude Code / 别的 CLI：改 `ExternalCLI.cmd` 与 `extra_args` 即可
- **成本提醒**：外部 CLI 单题约 46k token（冻结 harness 约 8k），扩样本前先估预算

## 5. 加一族新任务

在 `aipk/tasks/` 加 `fNN_xxx.py`：

```python
from . import GradeResult, TaskInstance, register, rng

@register("myfamily")
def build(seed: int) -> TaskInstance:
    r = rng(seed)
    answer = r.randint(1, 100)          # 参考实现现场算出真值
    def grader(text: str, **_kw) -> GradeResult:
        ok = str(answer) in text
        return GradeResult(ok, 1.0 if ok else 0.0, {"hit": ok}, "对" if ok else "错")
    return TaskInstance(tid=f"z{seed}", family="myfamily", seed=seed,
                        messages=[{"role": "user", "content": "..."}],
                        answer_spec="整数", grader=grader)
```

文件会被自动发现。**然后必须做三件事**：

1. 在 `aipk/oracle.py::truth_answer` 加一条，让 `selfcheck` 能验证"真值不会被判错"
2. 在 `tests/test_measurement.py` 加反套利测试（至少：明显错的答案要判错）
3. `python -m aipk selfcheck` + `python tests/test_measurement.py` + `python -m aipk demo` 全绿

报告里的族名标签在 `aipk/report.py::FAMILY_LABEL`。

## 6. 常见坑

- Windows 上改含中文的 UTF-8 文件**不要**用 PowerShell 的 `Get-Content`/`Set-Content`
  做字符串替换（PS5.1 按 GBK 解码会把整份文件写坏），用编辑器或 Python
- PowerShell 读 UTF-8 JSON 会乱码 → 用 Python 读
- 日志重定向用 `*>&1 | Out-File -Encoding utf8`
- 有些网关不接受某些采样参数（例如 kimi-k3 拒绝 `temperature=0.7`）：
  本项目会自动降级重投，并在报告里标注「参数降级」—— 这些模型的稳定性指标不能与别人直接比
