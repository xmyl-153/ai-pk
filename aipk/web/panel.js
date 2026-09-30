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
  $("#aboutView").hidden = mode !== "about";
  $("#tabScore").classList.toggle("active", mode === "score");
  $("#tabPk").classList.toggle("active", mode === "pk");
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
      <div><span class="k">联系</span><a href="${esc(a.issues)}" target="_blank" rel="noopener">提 Issue / PR</a></div>
    </div>
    <p class="about-contact">${esc(a.contact)}</p>`;
}
async function checkUpdate() {
  const el = $("#updateResult");
  el.textContent = "检查中…";
  const r = await (await fetch("/api/update")).json();
  if (!r.ok) { el.textContent = r.note || "检查失败"; return; }
  el.textContent = r.has_update
    ? `发现新版本 v${r.latest}（当前 v${r.current}），git pull 更新。`
    : `当前 v${r.current}，已是最新。`;
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
