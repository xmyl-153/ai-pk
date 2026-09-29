/* AI PK · 模型对战台 前端逻辑
   两种模式：
   - 跑分模式（默认进页）：挑 N 个选手，同一批题各跑一遍，按你选的权重出排行榜；改权重实时重排。
   - 比分模式：左右拖两个选手，PK 出胜方。
   加权是"你心里的尺子"：默认分高 ≠ 用起来好，权重一变，排名就变。 */

const $ = (s) => document.querySelector(s);

const state = {
  mode: "score",
  pool: [],                 // 所有可选选手 {key,name,kind,note,group}
  scoreSel: new Set(),      // 跑分模式选中的 key
  scoreModels: null,        // 最近一次跑分的每个模型指标（供实时重排）
  weights: { q: .40, s: .20, c: .20, r: .20 },
  perception: {},           // /api/perception
  left: null, right: null,
  running: false,
  timer: null,
};

const PRESETS = {
  "均衡":     { q: .40, s: .20, c: .20, r: .20 },
  "性能优先": { q: .65, s: .15, c: .10, r: .10 },
  "便宜优先": { q: .30, s: .15, c: .45, r: .10 },
  "速度优先": { q: .35, s: .45, c: .10, r: .10 },
};

// 真实案例：本仓库 runs/20260925-132828（630 次真实运行）里这两个模型的四维指标。
// 用来演示"实测 vs 大众印象"——不花钱就能看到认知校准卡长什么样。
const SAMPLE = {
  note: "示例数据来自本仓库 runs/20260925-132828（630 次真实运行，7 类任务、每模型 63 题）。想出自己的结论，挑真模型跑一遍。",
  models: [
    { key: "alibailian/deepseek-v4-pro", name: "DeepSeek V4 Pro", rate: .984, avg_ms: 39876, avg_tok: 7503, rea_ratio: .185, avg_turns: 1.6, solved: 62, total: 63 },
    { key: "jiyuanapi/glm-5.3-flash", name: "GLM 5.3 Flash", rate: 1.0, avg_ms: 58117, avg_tok: 6925, rea_ratio: .149, avg_turns: 1.5, solved: 63, total: 63 },
  ],
};

/* ---------------- 小工具 ---------------- */
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function md(s) { return esc(s).replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>"); }
function fmtMs(ms) { return ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(2) + " s"; }
function fmtTok(v) { return v == null ? "—" : Math.round(v).toLocaleString(); }
function fmtPct(v) { return v == null ? "—" : (v * 100).toFixed(1) + "%"; }

/* 加权合成：每个维度用"相对这一批里最优的比例"归一化（最优=1.0，其余按比例），
   没数据的维度给中性 0.5。比 min-max 更直观、且在模型很少时不会把差距放大成碾压。 */
function norm(models, field, higher) {
  const vals = models.map((m) => m[field]);
  const present = vals.filter((v) => v != null && isFinite(v));
  if (!present.length) return vals.map(() => 0.5);
  if (higher) {                                   // 越大越好（质量）
    const best = Math.max(...present);
    return vals.map((v) => (v == null || !isFinite(v) || best <= 0) ? 0.5 : v / best);
  }
  const best = Math.min(...present);              // 越小越好（速度 / 成本 / 推理占比）
  return vals.map((v) => (v == null || !isFinite(v) || v <= 0) ? 0.5
                       : (best <= 0 ? 1.0 : best / v));
}
function composite(models, w) {
  const Q = norm(models, "rate", true);
  const S = norm(models, "avg_ms", false);
  const C = norm(models, "avg_tok", false);
  const R = norm(models, "rea_ratio", false);
  return models.map((m, i) => ({ ...m, score: (w.q * Q[i] + w.s * S[i] + w.c * C[i] + w.r * R[i]) * 100 }));
}

function perceptionFor(key) {
  const low = (key || "").toLowerCase();
  for (const k in state.perception) {
    const e = state.perception[k];
    if (k === key || (e.match || []).some((m) => low.includes(m))) return { key: k, ...e };
  }
  return null;
}

/* ---------------- 选手池 ---------------- */
async function boot() {
  const [st, per] = await Promise.all([
    (await fetch("/api/state")).json(),
    (await fetch("/api/perception")).json(),
  ]);
  state.perception = per;
  state.pool = [
    ...(st.offline || []).map((m) => ({ ...m, group: "离线" })),
    ...(st.models || []).map((m) => ({ ...m, group: "真模型" })),
  ];
  renderScorePool();
  renderPkPool();
  const tag = $("#gatewayTag");
  if (st.usable) { tag.textContent = `已接 ${st.usable} 个真模型`; tag.classList.add("ok"); }
  else { tag.textContent = "没接真模型 · 先用离线机器人玩"; tag.classList.add("warn"); }
  $("#scoreNote").textContent = st.note || "";
  wire();
  setMode("score");
}

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
    el.addEventListener("click", () => { state.picked = state.picked === m.key ? null : m.key;
      document.querySelectorAll("#pkPool .chip").forEach((c) => c.classList.remove("picked"));
      if (state.picked) el.classList.add("picked"); });
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
  $("#btnScore").disabled = !(state.scoreSel.size >= 2 && !state.running);
}

/* ---------------- 模式切换 ---------------- */
function setMode(mode) {
  state.mode = mode;
  $("#scoreView").hidden = mode !== "score";
  $("#pkView").hidden = mode !== "pk";
  $("#tabScore").classList.toggle("active", mode === "score");
  $("#tabPk").classList.toggle("active", mode === "pk");
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

/* ---------------- 排行榜（跑分模式） ---------------- */
function renderLeaderboard() {
  if (!state.scoreModels) return;
  const rows = composite(state.scoreModels, state.weights).sort((a, b) => b.score - a.score);
  const tb = $("#leaderboard");
  tb.innerHTML = rows.map((m, i) => {
    const bar = Math.max(3, Math.round(m.score));
    return `<tr class="${i === 0 ? "top" : ""}">
      <td class="rank">${i + 1}</td>
      <td class="model">${esc(m.name)}${perceptionFor(m.key) ? ' <span class="pc" title="有认知校准说明">?</span>' : ""}</td>
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

function renderPerception(models, cardsId, sectionId) {
  const cards = models.filter((m) => perceptionFor(m.key));
  const sec = $(sectionId);
  if (!cards.length) { sec.hidden = true; return; }
  sec.hidden = false;
  $(cardsId).innerHTML = cards.map((m) => {
    const p = perceptionFor(m.key);
    const src = (p.sources || []).map((s) => `<a href="${s[1]}" target="_blank" rel="noopener">${esc(s[0])}</a>`).join(" · ");
    return `<div class="pcard">
      <div class="pcard-head">${esc(m.name)}</div>
      <p><b>大众印象：</b>${md(p.impression)}</p>
      <p><b>本工具实测：</b>${md(p.measured)}</p>
      <p><b>为什么对不上：</b>${md(p.why)}</p>
      <p><b>本工具的回应：</b>${md(p.response)}</p>
      ${src ? `<p class="psrc">出处：${src}</p>` : ""}
    </div>`;
  }).join("");
}

/* ---------------- 跑分模式：开一局 ---------------- */
function showSample() {
  state.scoreModels = SAMPLE.models;
  renderLeaderboard();
  renderPerception(SAMPLE.models, "#perceptionCards", "#scorePerception");
  $("#scoreNote").textContent = SAMPLE.note;
}

async function startScore() {
  state.running = true;
  refreshScoreBtn();
  $("#scoreResult").hidden = true; $("#scorePerception").hidden = true;
  $("#scoreProgress").hidden = false; $("#scoreLog").textContent = ""; $("#scoreBar").style.width = "0%";
  const start = await (await fetch("/api/score", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ models: [...state.scoreSel], scope: $("#scopeScore").value }),
  })).json();
  if (start.error) { finishScoreError(start.error); return; }
  pollRun(start.run_id, (r) => {
    $("#scoreBar").style.width = (r.total ? Math.round(r.done / r.total * 100) : 0) + "%";
    $("#scoreLog").textContent = (r.log || []).join("\n");
    $("#scoreLog").scrollTop = $("#scoreLog").scrollHeight;
    if (r.status === "done") {
      state.scoreModels = r.result.models;
      renderLeaderboard();
      renderPerception(r.result.models, "#perceptionCards", "#scorePerception");
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
  renderPerception([left, right], "#pkPerceptionCards", "#pkPerception");
}

async function startPk() {
  state.running = true; refreshFight();
  $("#aftermath").hidden = true; $("#verdict").hidden = true; $("#pkPerception").hidden = true;
  $("#progressBox").hidden = false; $("#log").textContent = ""; $("#barFill").style.width = "0%";
  $("#vs").classList.add("hot"); setTimeout(() => $("#vs").classList.remove("hot"), 520);
  const start = await (await fetch("/api/pk", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ left: state.left.key, right: state.right.key, scope: $("#scopePk").value }),
  })).json();
  if (start.error) { state.running = false; refreshFight(); $("#pkNote").textContent = start.error; return; }
  pollRun(start.run_id, (r) => {
    $("#barFill").style.width = (r.total ? Math.round(r.done / r.total * 100) : 0) + "%";
    $("#log").textContent = (r.log || []).join("\n");
    $("#log").scrollTop = $("#log").scrollHeight;
    if (r.status === "done") { renderPkResult(r); state.running = false; refreshFight(); $("#pkNote").textContent = "证据落在 " + r.result.out + "/"; }
    if (r.status === "error") { state.running = false; refreshFight(); $("#pkNote").textContent = r.error; }
  });
}

/* 轮询（两种模式共用，run_id 都在同一个 registry） */
function pollRun(runId, onTick) {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(async () => {
    const r = await (await fetch("/api/pk/" + runId)).json();
    onTick(r);
    if (r.status !== "running") clearInterval(state.timer);
  }, 700);
}

/* ---------------- 事件绑定 ---------------- */
function wire() {
  $("#tabScore").addEventListener("click", () => setMode("score"));
  $("#tabPk").addEventListener("click", () => setMode("pk"));
  $("#btnScore").addEventListener("click", startScore);
  $("#btnSample").addEventListener("click", showSample);
  document.querySelectorAll(".preset").forEach((b) => b.addEventListener("click", () => applyPreset(b.dataset.preset)));
  ["wq", "ws", "wc", "wr"].forEach((id) => $(("#" + id)).addEventListener("input", () => {
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
