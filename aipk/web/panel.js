/* AI PK · 模型对战台 前端逻辑
   三种页签：
   - 跑分模式（默认）：挑 1~8 个选手，同一批题各跑一遍，按你选的权重出排行榜；改权重实时重排。
   - 比分模式：左右拖两个选手 PK 出胜方。
   - 关于：版本 / 检查更新 / 联系作者。
   跑分是排队制：一次只跑一局，多提交的就排队等（避免一起压网关触发限流）。 */

const $ = (s) => document.querySelector(s);

const state = {
  mode: "score",
  pool: [],
  scoreSel: new Set(),
  scoreModels: null,
  weights: { q: .40, s: .20, c: .20, r: .20 },
  left: null, right: null,
  running: false,
  timer: null,
  aboutLoaded: false,
};

const PRESETS = {
  "均衡":     { q: .40, s: .20, c: .20, r: .20 },
  "性能优先": { q: .65, s: .15, c: .10, r: .10 },
  "便宜优先": { q: .30, s: .15, c: .45, r: .10 },
  "速度优先": { q: .35, s: .45, c: .10, r: .10 },
};

// 真实案例：本仓库 runs/20260925-132828（630 次真实运行 = 10 模型 × 63 题）按模型聚合的四维指标，
// 聚合口径与 _summarize() 一致（infra_failure 不计入）。只展示测量数据本身，用来说明"权重一变、排名就变"。
// 示例不固定点名任何一对模型：每次点「看真实案例」从池子里随机抽 2 个，再点一次换一对。
const SAMPLE = {
  note: "示例数据来自本仓库 runs/20260925-132828（630 次真实运行，10 模型 × 63 题）。点「看真实案例」每次随机抽 2 个模型；想出自己的结论，挑真模型跑一遍。",
  pool: [
    { key: "alibailian/deepseek-v4-pro", name: "DeepSeek V4 Pro", rate: .984, avg_ms: 39876, avg_tok: 7503, rea_ratio: .185, avg_turns: 3.7, solved: 62, total: 63 },
    { key: "jiyuanapi/qwen3.8-max", name: "Qwen3.8 Max", rate: 1.0, avg_ms: 43469, avg_tok: 8927, rea_ratio: .073, avg_turns: 4.5, solved: 63, total: 63 },
    { key: "jiyuanapi/glm-5.3", name: "GLM-5.3", rate: .905, avg_ms: 56765, avg_tok: 7726, rea_ratio: .235, avg_turns: 4.1, solved: 57, total: 63 },
    { key: "alibailian/kimi-k3", name: "Kimi K3", rate: 1.0, avg_ms: 41196, avg_tok: 3971, rea_ratio: .291, avg_turns: 3.8, solved: 63, total: 63 },
    { key: "jiyuanapi/seed-2.1-pro", name: "Seed 2.1 Pro", rate: .905, avg_ms: 166263, avg_tok: 19432, rea_ratio: .394, avg_turns: 5.6, solved: 57, total: 63 },
    { key: "jiyuanapi/longcat-2.0", name: "LongCat 2.0", rate: .921, avg_ms: 75879, avg_tok: 11767, rea_ratio: .201, avg_turns: 4.8, solved: 58, total: 63 },
    { key: "zcode-api-key/glm-4.7", name: "GLM-4.7", rate: .937, avg_ms: 47574, avg_tok: 5890, rea_ratio: .149, avg_turns: 3.5, solved: 59, total: 63 },
    { key: "jiyuanapi/deepseek-flash", name: "DeepSeek V4.1 (flash)", rate: .968, avg_ms: 23826, avg_tok: 8165, rea_ratio: .072, avg_turns: 4.5, solved: 61, total: 63 },
    { key: "jiyuanapi/glm-5.3-flash", name: "GLM-5.3 Flash", rate: 1.0, avg_ms: 58117, avg_tok: 6925, rea_ratio: .149, avg_turns: 4.2, solved: 63, total: 63 },
    { key: "alibailian/qwen3.8-flash", name: "Qwen3.8 Flash", rate: 1.0, avg_ms: 29283, avg_tok: 9342, rea_ratio: .111, avg_turns: 4.5, solved: 63, total: 63 },
  ],
};
/* 从示例池随机抽 2 个不同模型（无放回），示例不固定点名任何一对 */
function drawSample() {
  const pool = [...SAMPLE.pool];
  const a = pool.splice(Math.floor(Math.random() * pool.length), 1)[0];
  const b = pool.splice(Math.floor(Math.random() * pool.length), 1)[0];
  return [a, b];
}

/* ---------------- 小工具 ---------------- */
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function fmtMs(ms) { return ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(2) + " s"; }
function fmtTok(v) { return v == null ? "—" : Math.round(v).toLocaleString(); }
function fmtPct(v) { return v == null ? "—" : (v * 100).toFixed(1) + "%"; }

/* 加权合成：每个维度用"相对这一批里最优的比例"归一化（最优=1.0），没数据给中性 0.5 */
function norm(models, field, higher) {
  const vals = models.map((m) => m[field]);
  const present = vals.filter((v) => v != null && isFinite(v));
  if (!present.length) return vals.map(() => 0.5);
  if (higher) {
    const best = Math.max(...present);
    return vals.map((v) => (v == null || !isFinite(v) || best <= 0) ? 0.5 : v / best);
  }
  const best = Math.min(...present);
  return vals.map((v) => (v == null || !isFinite(v) || v <= 0) ? 0.5 : (best <= 0 ? 1.0 : best / v));
}
function composite(models, w) {
  const Q = norm(models, "rate", true);
  const S = norm(models, "avg_ms", false);
  const C = norm(models, "avg_tok", false);
  const R = norm(models, "rea_ratio", false);
  return models.map((m, i) => ({ ...m, score: (w.q * Q[i] + w.s * S[i] + w.c * C[i] + w.r * R[i]) * 100 }));
}

/* ---------------- 启动 ---------------- */
async function boot() {
  const st = await (await fetch("/api/state")).json();
  state.pool = [
    ...(st.offline || []).map((m) => ({ ...m, group: "离线" })),
    ...(st.models || []).map((m) => ({ ...m, group: "真模型" })),
  ];
  renderScorePool();
  renderPkPool();
  renderIdeas();
  const tag = $("#gatewayTag");
  if (st.usable) { tag.textContent = `已接 ${st.usable} 个真模型`; tag.classList.add("ok"); }
  else { tag.textContent = "没接真模型 · 点右上角接入"; tag.classList.add("warn"); }
  $("#scoreNote").textContent = st.note || "";
  wire();
  setMode("score");
  // 深链：/?sample 看随机抽的真实案例；/?sample=<key1>,<key2> 指定一对（分享 / 截图复现用）
  const sp = new URLSearchParams(location.search);
  if (sp.has("sample")) {
    const fixed = (sp.get("sample") || "").split(",").map((s) => s.trim()).filter(Boolean);
    showSample(fixed.length >= 2 ? fixed : null);
  }
  const tab = sp.get("tab");
  if (tab && ["score", "pk", "ideas", "about"].includes(tab)) setMode(tab);
}

/* ---------------- 选手池 ---------------- */
function chipEl(m, { score }) {
  const el = document.createElement("div");
  el.className = "chip" + (score ? " score-chip" : "") + (state.scoreSel.has(m.key) ? " picked" : "");
  el.dataset.key = m.key;
  el.innerHTML = `<div class="c-name">${esc(m.name)}<span class="c-kind">${esc(m.kind || m.group || "")}</span></div>
                  <div class="c-note">${esc(m.note || "")}</div>`;
  if (score) {
    el.addEventListener("click", () => toggleScoreSel(m.key));
  } else {
    el.draggable = true;
    el.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", m.key); e.dataTransfer.effectAllowed = "copy"; });
    el.addEventListener("click", () => {
      state.picked = state.picked === m.key ? null : m.key;
      document.querySelectorAll("#pkPool .chip").forEach((c) => c.classList.remove("picked"));
      if (state.picked) el.classList.add("picked");
    });
  }
  return el;
}
function fillPool(elId, { score }) {
  const box = $(elId);
  box.innerHTML = "";
  for (const g of ["离线", "真模型"]) {
    const items = state.pool.filter((m) => m.group === g);
    if (!items.length) continue;
    const t = document.createElement("p");
    t.className = "pool-title";
    t.textContent = g === "离线" ? "离线机器人（不花钱、不用 key）" : "接进来的真模型（会花钱）";
    box.appendChild(t);
    items.forEach((m) => box.appendChild(chipEl(m, { score })));
  }
}
function renderScorePool() { fillPool("#scorePool", { score: true }); }
function renderPkPool() { fillPool("#pkPool", { score: false }); }

function toggleScoreSel(key) {
  if (state.scoreSel.has(key)) state.scoreSel.delete(key);
  else if (state.scoreSel.size < 8) state.scoreSel.add(key);
  renderScorePool();
  refreshScoreBtn();
}
function refreshScoreBtn() {
  $("#btnScore").disabled = !(state.scoreSel.size >= 1 && !state.running);
}

/* ---------------- 页签 ---------------- */
function setMode(mode) {
  state.mode = mode;
  $("#scoreView").hidden = mode !== "score";
  $("#pkView").hidden = mode !== "pk";
  $("#ideasView").hidden = mode !== "ideas";
  $("#aboutView").hidden = mode !== "about";
  $("#tabScore").classList.toggle("active", mode === "score");
  $("#tabPk").classList.toggle("active", mode === "pk");
  $("#tabIdeas").classList.toggle("active", mode === "ideas");
  $("#tabAbout").classList.toggle("active", mode === "about");
  if (mode === "about" && !state.aboutLoaded) loadAbout();
}

/* ---------------- 关于 / 更新 ---------------- */
async function loadAbout() {
  const a = await (await fetch("/api/about")).json();
  state.aboutLoaded = true;
  $("#aboutBody").innerHTML = `
    <div class="about-grid">
      <div><span class="k">版本</span><b>v${esc(a.version)}</b></div>
      <div><span class="k">作者</span><b>${esc(a.author)}</b></div>
      <div><span class="k">主页</span><a href="${esc(a.homepage)}" target="_blank" rel="noopener">${esc(a.homepage)}</a></div>
      <div><span class="k">博客</span><a href="${esc(a.blog)}" target="_blank" rel="noopener">${esc(a.blog)}</a></div>
      <div><span class="k">联系</span><a href="${esc(a.issues)}" target="_blank" rel="noopener">提 Issue / PR</a></div>
    </div>
    <p class="about-contact">${esc(a.contact)}</p>`;
}
async function checkUpdate() {
  const el = $("#updateResult");
  el.textContent = "检查中…";
  const r = await (await fetch("/api/update")).json();
  el.textContent = r.note || (!r.ok ? "检查失败"
    : (r.has_update ? `发现新版本 v${r.latest}（当前 v${r.current}），git pull 更新。`
                    : `当前 v${r.current}，已是最新。`));
}

/* ---------------- 测试建议池 ----------------
   每条：title 名字 / tags 主要摸什么 / prompt 可直接复制的提示词 /
   look 看什么 / limit 为什么它当不了严谨测量。
   这页不参与跑分：它负责"摸手感"，跑分模式负责"下结论"。 */
const IDEAS = [
  {
    title: "鹈鹕骑自行车",
    tags: ["空间构图", "网络名梗"],
    prompt: "请你创建一个html，内容是SVG绘制一个鹈鹕骑自行车的画面",
    look: ["要素齐不齐：鹈鹕的大嘴和喉囊、自行车的两个轮 / 车把 / 脚踏",
           "关系对不对：是\"骑\"在车座上，不是并排站着",
           "存成 .html 双击打开 —— 真的能渲染出来"],
    limit: "网络名梗提示词，模型可能在训练数据里背过模板；画得好也许是默写，不是能力。",
  },
  {
    title: "指向 4:20 的指针时钟",
    tags: ["精确几何"],
    prompt: "请你创建一个html，用SVG画一个指针式时钟，指针精确指向4点20分：分针指向4（120度）、时针在4和5之间（130度），不要用数字直接写时间",
    look: ["分针 120°、时针 130°（很多模型会把时针画成正指 4）",
           "时针 / 分针长短粗细可区分",
           "刻度是 12 个，不是 10 个或 14 个"],
    limit: "答案就是一道算术题；对了只说明它这次算对了，不代表几何能力整体强。",
  },
  {
    title: "九尾狐，正好九条尾巴",
    tags: ["计数", "指令跟随"],
    prompt: "请你创建一个html，用SVG画一只九尾狐：要求正好九条尾巴，不多不少，且每条都连在身体上",
    look: ["数尾巴：九条，不多不少",
           "尾巴是\"长\"在身上，不是飘在旁边的独立图形",
           "九条尾巴有没有糊成一团数不清"],
    limit: "计数是不少模型的已知弱项；但单案例只说明\"这次踩没踩坑\"，不出分数。",
  },
  {
    title: "猫钓鱼，倒影是鱼骨头",
    tags: ["隐喻理解", "场景层次"],
    prompt: "请你创建一个html，用SVG画一个场景：一只猫在河边钓鱼，水面里猫的倒影是猫自己，但钩上的鱼在水里的倒影是一副鱼骨头",
    look: ["懂不懂\"倒影 ≠ 实物\"这个反差（题眼）",
           "水面线分得清：岸上实景 / 水里倒影两层",
           "钩上的鱼本体还是活鱼 —— 只有倒影是骨头"],
    limit: "判分完全主观；你觉得它\"懂了隐喻\"，可能只是构图碰巧。",
  },
  {
    title: "纯 CSS 讲囚徒困境",
    tags: ["硬约束", "真能用"],
    prompt: "请你创建一个html，用纯CSS（完全不用JavaScript）演示\"囚徒困境\"：两个囚犯各自选择\"坦白/抵赖\"（用checkbox hack实现点击），页面给出四种组合的判刑结果",
    look: ["看源码搜 script：应该一个都没有",
           "四种组合的结果齐全且自洽（如双抵赖各 1 年、双坦白各 5 年、一坦白一抵赖 0 年 / 10 年）",
           "真的点得动：四种组合都能切换出来"],
    limit: "\"不用 JS\"能机器验证，\"讲没讲清楚\"只能人眼看 —— 一半严谨一半主观。",
  },
  {
    title: "一页诚实描述自己的网页",
    tags: ["自指", "自信校准"],
    prompt: "请你创建一个html，页面内容是对它自己源码的描述：至少三条可核对的量化陈述（例如总行数、某个字出现的次数、用了几种颜色），要求全部为真",
    look: ["逐条核对：它说的行数 / 字数 / 颜色数跟源码对得上吗",
           "模型常常非常自信地写出假的自我描述",
           "有没有用\"大约\"\"左右\"把陈述糊过去"],
    limit: "核对要你亲手数；它测的是\"敢不敢让自己被核对\"，不是知识量。",
  },
];

function renderIdeas() {
  $("#ideasList").innerHTML = IDEAS.map((d, i) => `
    <div class="result idea-card">
      <div class="idea-head">
        <h2>${i + 1}. ${esc(d.title)}</h2>
        <span class="idea-tags">${d.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</span>
      </div>
      <pre class="idea-prompt">${esc(d.prompt)}</pre>
      <div class="idea-row">
        <button class="mini" data-copy="${i}">复制提示词</button>
        <span class="note">粘给任何会写代码的模型 / agent，把产物存成 .html 打开看</span>
      </div>
      <div class="idea-cols">
        <div><span class="k">看什么</span><ul>${d.look.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
        <div><span class="k">局限</span><p>${esc(d.limit)}</p></div>
      </div>
    </div>`).join("");
  document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    const text = IDEAS[+b.dataset.copy].prompt;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove();
    }
    b.textContent = "已复制 ✓";
    setTimeout(() => { b.textContent = "复制提示词"; }, 1500);
  }));
}

/* ---------------- 加权 ---------------- */
function readWeights() {
  const w = { q: +$("#wq").value, s: +$("#ws").value, c: +$("#wc").value, r: +$("#wr").value };
  const sum = w.q + w.s + w.c + w.r;
  if (sum > 0) { w.q /= sum; w.s /= sum; w.c /= sum; w.r /= sum; }
  state.weights = w;
  $("#wqVal").textContent = Math.round(w.q * 100);
  $("#wsVal").textContent = Math.round(w.s * 100);
  $("#wcVal").textContent = Math.round(w.c * 100);
  $("#wrVal").textContent = Math.round(w.r * 100);
}
function applyPreset(name) {
  const p = PRESETS[name]; if (!p) return;
  $("#wq").value = p.q * 100; $("#ws").value = p.s * 100;
  $("#wc").value = p.c * 100; $("#wr").value = p.r * 100;
  document.querySelectorAll(".preset").forEach((b) => b.classList.toggle("active", b.dataset.preset === name));
  readWeights();
  if (state.scoreModels) renderLeaderboard();
}

/* ---------------- 排行榜 ---------------- */
function renderLeaderboard() {
  if (!state.scoreModels) return;
  const rows = composite(state.scoreModels, state.weights).sort((a, b) => b.score - a.score);
  $("#leaderboard").innerHTML = rows.map((m, i) => {
    const bar = Math.max(3, Math.round(m.score));
    return `<tr class="${i === 0 ? "lead" : ""}">
      <td class="rank">${i + 1}</td>
      <td class="model">${esc(m.name)}</td>
      <td class="score"><span class="scorebar"><i style="width:${bar}%"></i></span><b>${m.score.toFixed(1)}</b></td>
      <td>${(m.rate * 100).toFixed(1)}%（${m.solved}/${m.total}）</td>
      <td>${fmtMs(m.avg_ms)}</td>
      <td>${fmtTok(m.avg_tok)}</td>
      <td>${fmtPct(m.rea_ratio)}</td>
      <td>${m.avg_turns.toFixed(1)}</td>
    </tr>`;
  }).join("");
  $("#scoreResult").hidden = false;
}
function showSample(fixedKeys) {
  let pair = null;
  if (fixedKeys && fixedKeys.length >= 2) {
    const a = SAMPLE.pool.find((m) => m.key === fixedKeys[0]);
    const b = SAMPLE.pool.find((m) => m.key === fixedKeys[1]);
    if (a && b && a !== b) pair = [a, b];
  }
  state.scoreModels = pair || drawSample();
  renderLeaderboard();
  $("#scoreNote").textContent = SAMPLE.note;
  $("#scoreResult").scrollIntoView({ block: "end" });
}

/* ---------------- 跑分（排队制） ---------------- */
async function startScore() {
  state.running = true;
  refreshScoreBtn();
  $("#scoreResult").hidden = true;
  $("#scoreProgress").hidden = false; $("#scoreLog").textContent = ""; $("#scoreBar").style.width = "0%";
  const start = await (await fetch("/api/score", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ models: [...state.scoreSel], scope: $("#scopeScore").value }),
  })).json();
  if (start.error) { finishScoreError(start.error); return; }
  pollRun(start.run_id, (r) => {
    $("#scoreBar").style.width = (r.total ? Math.round((r.done || 0) / r.total * 100) : 0) + "%";
    $("#scoreLog").textContent = (r.log || []).join("\n");
    $("#scoreLog").scrollTop = $("#scoreLog").scrollHeight;
    if (r.status === "done") {
      state.scoreModels = r.result.models;
      renderLeaderboard();
      finishScore();
    }
    if (r.status === "error") finishScoreError(r.error);
  });
}
function finishScore() { state.running = false; refreshScoreBtn(); }
function finishScoreError(msg) { state.running = false; refreshScoreBtn(); $("#scoreNote").textContent = msg; }

/* ---------------- 比分模式 ---------------- */
function setSide(side, key) {
  const m = state.pool.find((x) => x.key === key);
  if (!m) return;
  state[side] = m;
  const box = $(side === "left" ? "#slotLeft" : "#slotRight");
  box.querySelector(".slot-empty").hidden = true;
  const f = box.querySelector(".fighter"); f.hidden = false;
  f.querySelector(".fighter-name").textContent = m.name;
  f.querySelector(".fighter-meta").textContent = m.note || m.kind || "";
  $(side === "left" ? "#statsLeft" : "#statsRight").hidden = true;
  $(side === "left" ? "#sideLeft" : "#sideRight").classList.remove("win");
  refreshFight();
}
function clearSide(side) {
  state[side] = null;
  const box = $(side === "left" ? "#slotLeft" : "#slotRight");
  box.querySelector(".slot-empty").hidden = false;
  box.querySelector(".fighter").hidden = true;
  $(side === "left" ? "#statsLeft" : "#statsRight").hidden = true;
  $(side === "left" ? "#sideLeft" : "#sideRight").classList.remove("win");
  refreshFight();
}
function refreshFight() { $("#btnFight").disabled = !(state.left && state.right && !state.running); }

function renderPkResult(r) {
  const { left, right } = r.result;
  ["left", "right"].forEach((side) => {
    const d = side === "left" ? left : right;
    const stats = $(side === "left" ? "#statsLeft" : "#statsRight");
    stats.hidden = false;
    stats.querySelector(".big .num").textContent = Math.round(d.rate * 100);
    const cells = stats.querySelector(".cells"); cells.innerHTML = "";
    d.cells.forEach((c) => { const el = document.createElement("span"); el.className = "cell " + c;
      el.title = c === "ok" ? "答对" : (c === "inf" ? "基础设施故障（不计分）" : "答错"); cells.appendChild(el); });
    stats.querySelector(".rows").innerHTML = `
      <div><span>拿下</span><b>${d.solved} / ${d.total} 题</b></div>
      <div><span>平均耗时</span><b>${fmtMs(d.avg_ms)}</b></div>
      <div><span>平均 token</span><b>${fmtTok(d.avg_tok)}</b></div>
      <div><span>推理占比</span><b>${fmtPct(d.rea_ratio)}</b></div>`;
  });
  const v = $("#verdict"); v.hidden = false; v.textContent = r.result.headline;
  if (r.result.verdict === "left") $("#sideLeft").classList.add("win");
  if (r.result.verdict === "right") $("#sideRight").classList.add("win");
  $("#aftermathBody").innerHTML = "<ul>" + r.result.bullets.map((b) => `<li>${b}</li>`).join("") + "</ul>";
  $("#aftermath").hidden = false;
}

async function startPk() {
  state.running = true; refreshFight();
  $("#aftermath").hidden = true; $("#verdict").hidden = true;
  $("#progressBox").hidden = false; $("#log").textContent = ""; $("#barFill").style.width = "0%";
  $("#vs").classList.add("hot"); setTimeout(() => $("#vs").classList.remove("hot"), 520);
  const start = await (await fetch("/api/pk", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ left: state.left.key, right: state.right.key, scope: $("#scopePk").value }),
  })).json();
  if (start.error) { state.running = false; refreshFight(); $("#pkNote").textContent = start.error; return; }
  pollRun(start.run_id, (r) => {
    $("#barFill").style.width = (r.total ? Math.round((r.done || 0) / r.total * 100) : 0) + "%";
    $("#log").textContent = (r.log || []).join("\n");
    $("#log").scrollTop = $("#log").scrollHeight;
    if (r.status === "done") { renderPkResult(r); state.running = false; refreshFight(); $("#pkNote").textContent = "证据落在 " + r.result.out + "/"; }
    if (r.status === "error") { state.running = false; refreshFight(); $("#pkNote").textContent = r.error; }
  });
}

/* 轮询（跑分 / 比分共用同一个 run registry；queued → running → done） */
function pollRun(runId, onTick) {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(async () => {
    const r = await (await fetch("/api/pk/" + runId)).json();
    onTick(r);
    if (r.status !== "running" && r.status !== "queued") clearInterval(state.timer);
  }, 700);
}

/* ---------------- 事件 ---------------- */
function wire() {
  $("#tabScore").addEventListener("click", () => setMode("score"));
  $("#tabPk").addEventListener("click", () => setMode("pk"));
  $("#tabIdeas").addEventListener("click", () => setMode("ideas"));
  $("#tabAbout").addEventListener("click", () => setMode("about"));
  $("#btnConfig").addEventListener("click", () => { $("#configGuide").hidden = !$("#configGuide").hidden; });
  $("#guideClose").addEventListener("click", () => { $("#configGuide").hidden = true; });
  $("#btnUpdate").addEventListener("click", checkUpdate);
  $("#btnScore").addEventListener("click", startScore);
  $("#btnSample").addEventListener("click", () => showSample());
  document.querySelectorAll(".preset").forEach((b) => b.addEventListener("click", () => applyPreset(b.dataset.preset)));
  ["wq", "ws", "wc", "wr"].forEach((id) => $("#" + id).addEventListener("input", () => {
    document.querySelectorAll(".preset").forEach((b) => b.classList.remove("active"));
    readWeights();
    if (state.scoreModels) renderLeaderboard();
  }));
  ["left", "right"].forEach((side) => {
    const box = $(side === "left" ? "#slotLeft" : "#slotRight");
    box.addEventListener("dragover", (e) => { e.preventDefault(); box.classList.add("hot"); });
    box.addEventListener("dragleave", () => box.classList.remove("hot"));
    box.addEventListener("drop", (e) => { e.preventDefault(); box.classList.remove("hot");
      const key = e.dataTransfer.getData("text/plain"); if (key) setSide(side, key); });
    box.addEventListener("click", () => { if (state.picked) { setSide(side, state.picked);
      state.picked = null; document.querySelectorAll("#pkPool .chip").forEach((c) => c.classList.remove("picked")); } });
    $(`[data-act="clear"][data-side="${side}"]`).addEventListener("click", (e) => { e.stopPropagation(); clearSide(side); });
  });
  $("#btnRandom").addEventListener("click", () => {
    const pool = [...state.pool].sort(() => Math.random() - 0.5);
    if (pool.length < 2) return;
    setSide("left", pool[0].key); setSide("right", pool[1].key);
  });
  $("#btnFight").addEventListener("click", startPk);
}

boot();
