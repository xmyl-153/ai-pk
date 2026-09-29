/* AI PK · 对战台 前端逻辑
   规则：左边一队、右边一队，同一批题各跑一遍，谁通过率高谁赢；
   分数拉不开就看谁快、谁省 —— 这正是这个项目想让人看见的东西。 */

const $ = (sel) => document.querySelector(sel);

const state = {
  picked: null,          // 从选手池里点中的 key
  left: null,            // {key, name, note, kind}
  right: null,
  models: { offline: [], real: [] },
  running: false,
  timer: null,
};

const SIDE = {
  left: { slot: "#slotLeft", stats: "#statsLeft", box: "#sideLeft" },
  right: { slot: "#slotRight", stats: "#statsRight", box: "#sideRight" },
};

/* ---------------- 载入选手池 ---------------- */
async function boot() {
  const st = await (await fetch("/api/state")).json();
  state.models = { offline: st.offline || [], real: st.models || [] };
  renderPool();
  const tag = $("#gatewayTag");
  if (st.usable) {
    tag.textContent = `已接 ${st.usable} 个模型`;
    tag.classList.add("ok");
  } else {
    tag.textContent = "没接真模型 · 先用离线机器人玩";
    tag.classList.add("warn");
  }
  $("#note").textContent = st.note || "";
}

function chipEl(m) {
  const el = document.createElement("div");
  el.className = "chip" + (m.enabled === false ? " disabled" : "");
  el.draggable = m.enabled !== false;
  el.dataset.key = m.key;
  el.innerHTML = `
    <div class="c-name">${esc(m.name)}<span class="c-kind">${esc(m.kind || "")}</span></div>
    <div class="c-note">${esc(m.note || "")}</div>`;
  if (m.enabled === false) return el;
  el.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/plain", m.key);
    e.dataTransfer.effectAllowed = "copy";
  });
  el.addEventListener("click", () => {
    state.picked = state.picked === m.key ? null : m.key;
    document.querySelectorAll(".chip").forEach((c) => c.classList.remove("picked"));
    if (state.picked) el.classList.add("picked");
  });
  return el;
}

function renderPool() {
  const pool = $("#pool");
  pool.innerHTML = "";
  const add = (title, list) => {
    if (!list.length) return;
    const t = document.createElement("p");
    t.className = "pool-title";
    t.textContent = title;
    pool.appendChild(t);
    list.forEach((m) => pool.appendChild(chipEl(m)));
  };
  add("离线机器人（不花钱、不用 key）", state.models.offline);
  add("接进来的模型（要配网关才会有）", state.models.real);
}

const findModel = (key) =>
  [...state.models.offline, ...state.models.real].find((m) => m.key === key);

/* ---------------- 放置 ---------------- */
function setSide(side, key) {
  const m = findModel(key);
  if (!m || m.enabled === false) return;
  state[side] = m;
  const { slot, stats } = SIDE[side];
  const box = $(slot);
  box.querySelector(".slot-empty").hidden = true;
  const f = box.querySelector(".fighter");
  f.hidden = false;
  f.querySelector(".fighter-name").textContent = m.name;
  f.querySelector(".fighter-meta").textContent = m.note || m.kind || "";
  $(stats).hidden = true;
  $(SIDE[side].box).classList.remove("win");
  refresh();
}

function clearSide(side) {
  state[side] = null;
  const { slot, stats, box } = SIDE[side];
  const b = $(slot);
  b.querySelector(".slot-empty").hidden = false;
  b.querySelector(".fighter").hidden = true;
  $(stats).hidden = true;
  $(box).classList.remove("win");
  refresh();
}

function refresh() {
  const ok = state.left && state.right && !state.running;
  $("#btnFight").disabled = !ok;
  $("#verdict").hidden = true;
  if (!ok && !state.running) {
    $("#aftermath").hidden = true;
    $("#progressBox").hidden = true;
  }
}

["left", "right"].forEach((side) => {
  const box = $(SIDE[side].slot);
  box.addEventListener("dragover", (e) => { e.preventDefault(); box.classList.add("hot"); });
  box.addEventListener("dragleave", () => box.classList.remove("hot"));
  box.addEventListener("drop", (e) => {
    e.preventDefault();
    box.classList.remove("hot");
    const key = e.dataTransfer.getData("text/plain");
    if (key) setSide(side, key);
  });
  box.addEventListener("click", () => {
    if (state.picked) {
      setSide(side, state.picked);
      state.picked = null;
      document.querySelectorAll(".chip").forEach((c) => c.classList.remove("picked"));
    }
  });
  document.querySelector(`[data-act="clear"][data-side="${side}"]`)
    .addEventListener("click", (e) => { e.stopPropagation(); clearSide(side); });
});

$("#btnRandom").addEventListener("click", () => {
  const pool = [...state.models.offline, ...state.models.real].filter((m) => m.enabled !== false);
  if (pool.length < 2) return;
  const shuffled = pool.sort(() => Math.random() - 0.5);
  setSide("left", shuffled[0].key);
  setSide("right", shuffled[1].key);
});

/* ---------------- 开打 ---------------- */
$("#btnFight").addEventListener("click", async () => {
  state.running = true;
  $("#btnFight").disabled = true;
  $("#aftermath").hidden = true;
  $("#verdict").hidden = true;
  $("#progressBox").hidden = false;
  $("#log").textContent = "";
  $("#barFill").style.width = "0%";
  $("#vs").classList.add("hot");
  setTimeout(() => $("#vs").classList.remove("hot"), 520);
  ["left", "right"].forEach((s) => { $(SIDE[s].stats).hidden = true; $(SIDE[s].box).classList.remove("win"); });

  const res = await fetch("/api/pk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ left: state.left.key, right: state.right.key, scope: $("#scope").value }),
  });
  const start = await res.json();
  if (start.error) { finishWithError(start.error); return; }
  poll(start.run_id);
});

function poll(runId) {
  state.timer = setInterval(async () => {
    const r = await (await fetch(`/api/pk/${runId}`)).json();
    $("#log").textContent = (r.log || []).join("\n");
    $("#log").scrollTop = $("#log").scrollHeight;
    const pct = r.total ? Math.round((r.done / r.total) * 100) : 0;
    $("#barFill").style.width = pct + "%";
    if (r.status === "done") { clearInterval(state.timer); showResult(r); }
    if (r.status === "error") { clearInterval(state.timer); finishWithError(r.error || "跑挂了"); }
  }, 700);
}

function finishWithError(msg) {
  state.running = false;
  $("#note").textContent = msg;
  refresh();
}

/* ---------------- 结果展示 ---------------- */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function showResult(r) {
  state.running = false;
  const { left, right } = r.result;
  ["left", "right"].forEach((side) => {
    const d = side === "left" ? left : right;
    const stats = $(SIDE[side].stats);
    stats.hidden = false;
    stats.querySelector(".big .num").textContent = Math.round(d.rate * 100);
    const cells = stats.querySelector(".cells");
    cells.innerHTML = "";
    d.cells.forEach((c) => {
      const el = document.createElement("span");
      el.className = "cell " + c;
      el.title = c === "ok" ? "答对" : (c === "inf" ? "基础设施故障（不计分）" : "答错");
      cells.appendChild(el);
    });
    stats.querySelector(".rows").innerHTML = `
      <div><span>拿下一共</span><b>${d.solved} / ${d.total} 题</b></div>
      <div><span>平均耗时</span><b>${d.avg_ms < 1 ? "<1 ms" : (d.avg_ms / 1000).toFixed(2) + " s"}</b></div>
      <div><span>平均轮数</span><b>${d.avg_turns.toFixed(1)}</b></div>
      <div><span>token</span><b>${d.tokens === null ? "离线机器人不上报" : d.tokens}</b></div>`;
  });

  const v = $("#verdict");
  v.hidden = false;
  v.textContent = r.result.headline;
  if (r.result.verdict === "left") $(SIDE.left.box).classList.add("win");
  if (r.result.verdict === "right") $(SIDE.right.box).classList.add("win");

  const body = $("#aftermathBody");
  body.innerHTML = "<ul>" + r.result.bullets.map((b) => `<li>${b}</li>`).join("") + "</ul>";
  $("#aftermath").hidden = false;
  $("#note").textContent = "打完收工。原始证据落在 " + r.out;
  refresh();
  $("#verdict").hidden = false;
  $("#aftermath").hidden = false;
}

boot();
