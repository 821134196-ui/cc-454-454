/* 消防站空气呼吸器气瓶充装与检测系统 — 前端（原生 JS，无框架） */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  view: "ledger",
  me: null,                 // 当前操作人
  personnel: [], tasks: [], cylinders: [], reminders: [],
  thresholds: {}, adapter: "",
  detailBatch: null,
};

/* ---------------- API ---------------- */
async function api(path, { method = "GET", body, raw, filename } = {}) {
  const headers = {};
  if (state.me) headers["X-Actor"] = state.me.person_id;
  let payload;
  if (raw) {
    payload = body;
    headers["Content-Type"] = raw.type || "application/octet-stream";
    if (filename) headers["X-Filename"] = encodeURIComponent(filename);
  } else if (body !== undefined) {
    payload = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { method, headers, body: payload });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data?.error?.message || `HTTP ${res.status}`),
    { code: data?.error?.code || "ERROR", status: res.status });
  return data;
}

/* ---------------- 通用 UI ---------------- */
let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3200);
}
async function guard(fn) {
  try { await fn(); } catch (e) { toast(e.message, "err"); }
}
const roleTag = (r) =>
  ({ FIREFIGHTER: "战斗员", DISPATCHER: "装备员", INSPECTOR: "检验员", SUPERVISOR: "装备主管" }[r] || r);
const statusInfo = {
  AVAILABLE: ["可领用", "green"], ISSUED: ["已领用", "blue"],
  PENDING_INSPECTION: ["待检查", "yellow"], PENDING_FILL: ["待充装", "yellow"],
  IN_BATCH: ["已入批", "blue"], FILLING: ["充装中", "yellow"],
  SETTLING: ["静置中", "yellow"], PENDING_PAIR: ["待配套", "yellow"],
  REPAIR: ["维修中", "red"], SEALED: ["封存", "red"], SCRAPPED: ["报废", "gray"],
};
const tag = (s) => {
  const [t, c] = statusInfo[s] || [s, "gray"];
  return `<span class="tag tag-${c}">${t}</span>`;
};
const dispName = {
  VALVE_DAMAGE: "瓶阀损伤", APPEARANCE_FAIL: "外观不合格", OVERDUE: "超期未检",
  FILL_LOW: "终压不足", RETEST_LEAK: "复测泄漏", PAIR_MISMATCH: "错误配套",
  FILL_INTERRUPTED: "充装中断", EMERGENCY_LOAN: "紧急借调", CONDEMN_PENDING: "待报废审议",
  SCRAPPED: "已报废",
};
function can(...roles) { return state.me && roles.includes(state.me.role); }
function isOps() { return can("DISPATCHER", "INSPECTOR", "SUPERVISOR"); }
function isSupervisor() { return can("SUPERVISOR"); }

function pressureClass(p, rated) {
  if (p == null) return ["p-zero", "p-mid-txt", "—"];
  const r = p / rated;
  if (r >= 0.9) return ["p-ok", "p-ok-txt", ""];
  if (r >= 0.5) return ["p-mid", "p-mid-txt", ""];
  return ["p-low", "p-low-txt", ""];
}
const fmtTs = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";

/* ---------------- 初始化 ---------------- */
async function boot() {
  const data = await api("/api/bootstrap");
  Object.assign(state, {
    personnel: data.personnel, tasks: data.tasks, cylinders: data.cylinders,
    reminders: data.reminders, thresholds: data.thresholds, adapter: data.adapter,
  });
  $("#adapterName").textContent = "压力台：" + data.adapter;
  renderActorSelect();
  bindChrome();
  render();
  setInterval(refreshReminders, 8000);
  setInterval(() => { if (["batches", "retests"].includes(state.view)) render(); }, 15000);
}
function renderActorSelect() {
  const groups = {};
  for (const p of state.personnel) (groups[p.role] ||= []).push(p);
  const order = ["SUPERVISOR", "DISPATCHER", "INSPECTOR", "FIREFIGHTER"];
  $("#actorSelect").innerHTML = order.filter(r => groups[r]).map(r =>
    `<optgroup label="${roleTag(r)}">${groups[r].map(p =>
      `<option value="${p.person_id}">${esc(p.name)} · ${p.person_id}</option>`).join("")}
    </optgroup>`).join("");
  const saved = localStorage.getItem("scba_actor");
  if (saved && state.personnel.some(p => p.person_id === saved))
    $("#actorSelect").value = saved;
  state.me = state.personnel.find(p => p.person_id === $("#actorSelect").value);
}
function bindChrome() {
  $("#actorSelect").addEventListener("change", async () => {
    state.me = state.personnel.find(p => p.person_id === $("#actorSelect").value);
    localStorage.setItem("scba_actor", state.me.person_id);
    render();
  });
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest(".tab");
    if (!b) return;
    state.view = b.dataset.view;
    state.detailBatch = null;
    $$(".tab").forEach(t => t.classList.toggle("active", t === b));
    render();
  });
  $("#bellBtn").addEventListener("click", toggleReminderPanel);
  $("#drawerMask").addEventListener("click", closeDrawer);
}

/* ---------------- 提醒 ---------------- */
async function refreshReminders() {
  state.reminders = await api("/api/reminders");
  const n = state.reminders.length;
  const bc = $("#bellCount");
  bc.textContent = n;
  bc.classList.toggle("hidden", n === 0);
  if ($("#remPanel")) renderReminderPanel();
  if (state.view === "retests" || state.view === "batches") render();
}
function toggleReminderPanel() {
  const ex = $("#remPanel");
  if (ex) { ex.remove(); return; }
  const panel = document.createElement("div");
  panel.id = "remPanel";
  panel.className = "rem-panel";
  document.body.appendChild(panel);
  renderReminderPanel();
  setTimeout(() => document.addEventListener("click", function h(e) {
    if (!e.target.closest("#remPanel") && !e.target.closest("#bellBtn")) {
      $("#remPanel")?.remove();
      document.removeEventListener("click", h);
    }
  }), 0);
}
function renderReminderPanel() {
  const panel = $("#remPanel");
  if (!panel) return;
  panel.innerHTML = `<div style="font-weight:800;margin-bottom:8px">到期提醒（${state.reminders.length}）</div>` +
    (state.reminders.length === 0 ? `<p class="muted">暂无提醒</p>` :
      state.reminders.map(r => {
        const red = r.type.includes("OVERDUE") || r.type === "SETTLE_DUE";
        return `<div class="rem-item">
          <span class="tag tag-${red ? "red" : "yellow"}">${
            r.type === "SETTLE_DUE" ? "复测" : "检验"}</span>
          <p>${esc(r.message)}<br><span class="muted">${r.ts_str}</span></p>
          <button class="btn btn-sm btn-ghost" data-ack="${r.id}">忽略</button></div>`;
      }).join(""));
  $$("[data-ack]", panel).forEach(b => b.addEventListener("click", () => guard(async () => {
    await api(`/api/reminders/${b.dataset.ack}/ack`, { method: "POST", body: {} });
    await refreshReminders();
    toast("提醒已确认", "ok");
  })));
}

/* ---------------- 渲染路由 ---------------- */
const VIEWS = {
  ledger: viewLedger, issues: viewIssues, returns: viewReturns, batches: viewBatches,
  retests: viewRetests, pairs: viewPairs, due: viewDue, events: viewEvents,
};
async function render() {
  await guard(async () => {
    state.cylinders = await api("/api/cylinders");
    await VIEWS[state.view]($("#view"));
  });
}
function meLine() {
  return `<p class="muted" style="margin:0 0 12px">当前操作人：<b style="color:var(--yellow)">
    ${esc(state.me.name)}（${roleTag(state.me.role)} · ${state.me.person_id}）</b>
    ${isSupervisor() ? "｜可执行解封与报废确认" : isOps() ? "｜可执行充装检查操作" : "｜可扫码归队"}</p>`;
}

/* ================= 1. 装备台账（紧凑仪表行） ================= */
async function viewLedger(el) {
  const rated = state.thresholds.rated;
  const counts = {};
  state.cylinders.forEach(c => counts[c.status] = (counts[c.status] || 0) + 1);
  el.innerHTML = `
    <div class="page-head"><h2>装备台账 · 空气呼吸器气瓶</h2>
      <div class="muted">额定 ${rated} MPa ｜ 终压下限 ${state.thresholds.final_min} MPa ｜
        静置压降 ≤ ${state.thresholds.settle_drop_max} MPa ｜ 检验周期 ${state.thresholds.inspection_months} 月</div></div>
    <div class="grid-3" style="margin-bottom:14px">
      ${[["AVAILABLE", "可领用"], ["ISSUED", "在途领用"], ["PENDING_INSPECTION", "待检查"],
         ["PENDING_FILL", "待充装"], ["REPAIR", "维修中"], ["SEALED", "封存"],
         ["SCRAPPED", "报废"], ["SETTLING", "静置中"]].map(([s, n]) =>
        `<div class="card" style="padding:12px 16px"><div class="muted">${n}</div>
          <div style="font-size:26px;font-weight:900">${counts[s] || 0}</div>
          <div>${tag(s)}</div></div>`).join("")}
    </div>
    <div class="field"><select id="fStatus">
      <option value="">全部状态</option>
      ${Object.entries(statusInfo).map(([k, [v]]) => `<option value="${k}">${v}</option>`).join("")}
    </select></div>
    <div id="ledgerList"></div>`;
  const draw = () => {
    const f = $("#fStatus").value;
    const list = state.cylinders.filter(c => !f || c.status === f);
    $("#ledgerList").innerHTML = list.map(c => {
      const p = c.current_pressure;
      const [bc, tc] = pressureClass(p, rated);
      const pct = p == null ? 0 : Math.min(100, Math.round(p / rated * 100));
      const warn = c.overdue ? `<span class="tag tag-red">检验超期</span>`
        : c.due_soon ? `<span class="tag tag-yellow">${c.days_to_due}天到期</span>` : "";
      return `<div class="ledger-row" data-cyl="${c.cyl_no}">
        <div><div class="ledger-no">${c.cyl_no}</div>
          <div class="ledger-frame">背架 ${c.frame_no} · ${c.volume_l}L</div></div>
        <div>${tag(c.status)}${warn}</div>
        <div class="pbar ${bc}"><i style="width:${pct}%"></i>
          <span class="pbar-note">检验有效期 ${c.inspection_due}</span></div>
        <div class="ledger-pressure ${tc}">${p == null ? "—" : p.toFixed(1)}<small> MPa</small></div>
        <div class="muted">${c.disposition ? `⚠ ${dispName[c.disposition] || c.disposition}` : "查看 ›"}</div>
      </div>`;
    }).join("") || `<p class="muted">无匹配气瓶</p>`;
    $$("[data-cyl]", $("#ledgerList")).forEach(r =>
      r.addEventListener("click", () => openCylDrawer(r.dataset.cyl)));
  };
  $("#fStatus").addEventListener("change", draw);
  draw();
}

async function openCylDrawer(cylNo) {
  const d = await api(`/api/cylinders/${encodeURIComponent(cylNo)}`);
  const dr = $("#drawer");
  const isOverdue = new Date(d.inspection_due + "T00:00:00") < new Date(new Date().toDateString());
  dr.innerHTML = `
    <button class="btn btn-sm btn-ghost close" id="drawerClose">关闭 ✕</button>
    <h2>${d.cyl_no}</h2>
    <p class="muted">${d.material} ${d.volume_l}L · 额定 ${d.rated_pressure} MPa</p>
    <dl class="kv">
      <dt>所属背架</dt><dd>${d.frame_no}</dd>
      <dt>当前状态</dt><dd>${tag(d.status)} ${d.disposition ? `· ${dispName[d.disposition] || d.disposition}` : ""}</dd>
      <dt>当前压力</dt><dd class="num">${d.current_pressure ?? "—"} MPa</dd>
      <dt>上次检测</dt><dd>${d.last_inspection}</dd>
      <dt>有效期至</dt><dd>${d.inspection_due} ${isOverdue ? '<span class="tag tag-red">已超期</span>' : ""}</dd>
    </dl>
    <div class="checkline">
      ${d.status === "SEALED" && isSupervisor() ? `<button class="btn btn-yellow btn-sm" data-act="unseal">解除封存（主管）</button>` : ""}
      ${d.status !== "SCRAPPED" && isSupervisor() ? `<button class="btn btn-primary btn-sm" data-act="scrap">报废确认（主管）</button>` : ""}
      ${d.status !== "SCRAPPED" && d.status !== "SEALED" && isSupervisor() ? `<button class="btn btn-sm" data-act="seal">封存</button>` : ""}
      ${(can("INSPECTOR", "SUPERVISOR")) ? `<button class="btn btn-sm" data-act="insp">更新检验记录</button>` : ""}
    </div>
    <h3 style="font-size:14px;margin:14px 0 8px">检测附件（本地留存）</h3>
    <div>${d.attachments.length ? d.attachments.map(a =>
      `<div class="att-line"><span>📎 ${esc(a.filename)}</span>
        <a class="btn btn-sm btn-ghost" href="/api/attachments/${a.id}/download">下载</a></div>`).join("")
      : `<p class="muted">暂无附件</p>`}
      <label class="btn btn-sm" style="display:inline-block;margin-top:6px">上传附件
        <input type="file" id="attFile" hidden></label>
    </div>
    <h3 style="font-size:14px;margin:14px 0 8px">全程事件</h3>
    ${d.events.length ? d.events.map(e => `<div class="event-line"><b>${e.type}</b> · ${e.ts_str}
      ${e.actor_id ? `· ${e.actor_id}` : ""}${e.payload ? `<br><span class="muted">${esc(e.payload)}</span>` : ""}</div>`).join("")
      : `<p class="muted">无事件</p>`}`;
  dr.classList.remove("hidden");
  $("#drawerMask").classList.remove("hidden");
  $("#drawerClose").onclick = closeDrawer;
  $$("[data-act]", dr).forEach(b => b.addEventListener("click", () => guard(async () => {
    if (b.dataset.act === "unseal") {
      const reason = prompt("解除封存原因（如检验合格、复检通过）：", "复检合格，解除封存");
      if (reason === null) return;
      await api(`/api/cylinders/${d.cyl_no}/unseal`, { method: "POST", body: { reason } });
      toast("已解除封存，进入待充装", "ok");
    } else if (b.dataset.act === "scrap") {
      if (!confirm(`确认对 ${d.cyl_no} 执行报废确认？此操作仅装备主管可执行且不可撤销。`)) return;
      const reason = prompt("报废原因：", "主管评审确认报废");
      if (!reason) return;
      await api(`/api/cylinders/${d.cyl_no}/scrap`, { method: "POST", body: { reason } });
      toast("报废确认完成，气瓶已销账", "ok");
    } else if (b.dataset.act === "seal") {
      const reason = prompt("封存原因：", "装备封存");
      if (!reason) return;
      await api(`/api/cylinders/${d.cyl_no}/seal`, { method: "POST", body: { reason } });
      toast("已封存", "ok");
    } else if (b.dataset.act === "insp") {
      const v = prompt("登记新的上次水压检验日期（YYYY-MM-DD）：",
        new Date().toISOString().slice(0, 10));
      if (!v) return;
      const due = await api(`/api/cylinders/${d.cyl_no}/inspection`,
        { method: "POST", body: { last_inspection: v } });
      toast(`检验记录已更新，有效期至 ${due.inspection_due}`, "ok");
    }
    closeDrawer(); render();
  })));
  $("#attFile").addEventListener("change", async () => {
    const f = $("#attFile").files[0];
    if (!f) return;
    await guard(async () => {
      await api(`/api/cylinders/${d.cyl_no}/attachments`,
        { method: "POST", raw: f, filename: f.name });
      toast("附件已保存到本地目录", "ok");
      closeDrawer(); openCylDrawer(d.cyl_no);
    });
  });
}
function closeDrawer() {
  $("#drawer").classList.add("hidden");
  $("#drawerMask").classList.add("hidden");
}

/* ================= 2. 任务领用 / 紧急借调 ================= */
async function viewIssues(el) {
  const [issues] = await Promise.all([api("/api/issues")]);
  const avail = state.cylinders.filter(c => c.status === "AVAILABLE");
  const ff = state.personnel.filter(p => p.role === "FIREFIGHTER");
  el.innerHTML = `${meLine()}
    <div class="grid-2">
      <div class="card"><h2>领用登记 <span class="section-hint">封存/报废与在途气瓶不可领用</span></h2>
        <div class="field"><label>气瓶（仅可领用）</label><select id="iCyl">
          ${avail.length ? avail.map(c => `<option value="${c.cyl_no}">${c.cyl_no}（背架 ${c.frame_no}，${c.current_pressure?.toFixed(1)} MPa）</option>`).join("")
            : `<option value="">（无可领用气瓶）</option>`}</select></div>
        <div class="field"><label>领用类型</label><select id="iType">
          <option value="TASK">任务领用</option>
          <option value="EMERGENCY_LOAN">紧急借调（外单位）</option></select></div>
        <div class="field" id="taskField"><label>训练任务</label><select id="iTask">
          ${state.tasks.map(t => `<option value="${t.task_id}">${esc(t.name)} · ${t.location} · ${t.planned_at}</option>`).join("")}</select></div>
        <div class="field hidden" id="unitField"><label>借调去向单位</label>
          <input id="iUnit" placeholder="如：解放街消防救援站"></div>
        <div class="field"><label>领用人</label><select id="iPerson">
          ${ff.map(p => `<option value="${p.person_id}">${esc(p.name)} · ${p.unit}</option>`).join("")}</select></div>
        <button class="btn btn-primary btn-block" id="iGo" ${avail.length ? "" : "disabled"}>确认领用</button>
      </div>
      <div class="card"><h2>领用 / 借调记录</h2><div class="table-wrap"><table>
        <thead><tr><th>气瓶</th><th>类型</th><th>领用人</th><th>任务/去向</th><th>发出压力</th><th>归队余压</th><th>状态</th></tr></thead>
        <tbody>${issues.map(i => `<tr>
          <td><b>${i.cyl_no}</b></td>
          <td><span class="tag ${i.issue_type === "EMERGENCY_LOAN" ? "tag-red" : "tag-blue"}">${i.issue_type_label}</span></td>
          <td>${esc(i.person_name)}</td>
          <td>${i.issue_type === "EMERGENCY_LOAN" ? esc(i.target_unit) : esc(i.task_name || i.task_id)}</td>
          <td class="num">${i.issue_pressure ?? "—"}</td>
          <td class="num">${i.return_pressure ?? "—"}</td>
          <td>${i.status === "OPEN" ? '<span class="tag tag-yellow">在途</span>' : '<span class="tag tag-gray">已归队</span>'}</td>
        </tr>`).join("")}</tbody></table></div></div>
    </div>`;
  $("#iType").addEventListener("change", () => {
    const loan = $("#iType").value === "EMERGENCY_LOAN";
    $("#taskField").classList.toggle("hidden", loan);
    $("#unitField").classList.toggle("hidden", !loan);
  });
  $("#iGo").addEventListener("click", () => guard(async () => {
    const loan = $("#iType").value === "EMERGENCY_LOAN";
    const body = {
      cyl_no: $("#iCyl").value, issue_type: $("#iType").value,
      person_id: $("#iPerson").value,
      task_id: loan ? null : $("#iTask").value,
      target_unit: loan ? $("#iUnit").value.trim() : null,
    };
    await api("/api/issues", { method: "POST", body });
    toast(loan ? "紧急借调已登记" : "任务领用已登记", "ok");
    render();
  }));
}

/* ================= 3. 归队检查 ================= */
async function viewReturns(el) {
  const checks = await api("/api/checks");
  const out = state.cylinders.filter(c => c.status === "ISSUED");
  el.innerHTML = `${meLine()}
    <div class="grid-2" style="grid-template-columns:360px 1fr">
      <div class="card"><h2>扫码归队</h2>
        <p class="muted">多人同时扫描同一气瓶，系统只生成一条归队记录；重复扫描仅留痕。</p>
        <div class="field"><label>气瓶</label><select id="rCyl">
          ${out.length ? out.map(c => `<option value="${c.cyl_no}">${c.cyl_no}（背架 ${c.frame_no}）</option>`).join("")
            : `<option value="">（无在途气瓶）</option>`}</select></div>
        <div class="field"><label>归队余压 (MPa)</label><input id="rP" type="number" step="0.1" min="0" value="20.0"></div>
        <button class="btn btn-primary btn-block" id="rGo" ${out.length ? "" : "disabled"}>扫描归队</button>
        <hr style="border-color:var(--line);margin:16px 0">
        <div class="muted">合格条件：外观合格 <b>且</b> 瓶阀正常 <b>且</b> 在检验有效期内，方可加入充装批次。</div>
      </div>
      <div class="card"><h2>归队检查记录</h2>
        <div class="table-wrap"><table><thead><tr>
          <th>气瓶</th><th>余压</th><th>外观</th><th>瓶阀</th><th>有效期</th><th>判定</th><th>检查员</th><th>操作</th>
        </tr></thead><tbody>
        ${checks.map(c => `<tr>
          <td><b>${c.cyl_no}</b></td>
          <td class="num">${c.return_pressure}</td>
          <td>${c.appearance_ok == null ? "—" : c.appearance_ok ? "✓" : "✗"}</td>
          <td>${c.valve_ok == null ? "—" : c.valve_ok ? "✓" : "✗"}</td>
          <td>${c.within_due == null ? "—" : c.within_due ? "在期" : '<span class="tag tag-red">超期</span>'}</td>
          <td><span class="tag tag-${c.result === "PASS" ? "green" : c.result === "PENDING" ? "yellow" : "red"}">${c.result_label}</span></td>
          <td>${esc(c.inspector_name || "—")}</td>
          <td>${c.result === "PENDING" ? `
            <div class="checkline" style="margin:0">
              <label style="min-height:auto"><input type="checkbox" data-app="${c.id}" checked> 外观合格</label>
              <label style="min-height:auto"><input type="checkbox" data-val="${c.id}" checked> 瓶阀正常</label>
            </div>
            <button class="btn btn-sm btn-primary" data-check="${c.id}" ${isOps() ? "" : "disabled"}>提交检查</button>`
            : (c.attachment_id ? `<a class="btn btn-sm btn-ghost" href="/api/attachments/${c.attachment_id}/download">附件</a>` : "—")}
          </td></tr>`).join("")}</tbody></table></div>
        ${!isOps() ? `<p class="muted" style="margin-top:8px">战斗员仅可扫码归队，检查判定需装备员/检验员/主管权限。</p>` : ""}
      </div>
    </div>`;
  $("#rGo").addEventListener("click", () => guard(async () => {
    const res = await api("/api/returns", { method: "POST", body: {
      cyl_no: $("#rCyl").value, remain_pressure: parseFloat($("#rP").value) } });
    if (res.created) toast("归队记录已生成，进入待检查", "ok");
    else toast(res.message, "err");
    render();
  }));
  $$("[data-check]").forEach(b => b.addEventListener("click", () => guard(async () => {
    const id = b.dataset.check;
    await api(`/api/checks/${id}/inspect`, { method: "POST", body: {
      appearance_ok: $(`[data-app="${id}"]`).checked, valve_ok: $(`[data-val="${id}"]`).checked } });
    toast("检查已提交", "ok");
    render();
  })));
}

/* ================= 4. 充装组批 ================= */
async function viewBatches(el) {
  if (state.detailBatch) return viewBatchDetail(el, state.detailBatch);
  const batches = await api("/api/batches");
  const pending = state.cylinders.filter(c => c.status === "PENDING_FILL");
  el.innerHTML = `${meLine()}
    <div class="page-head"><h2>充装批次</h2>
      <div><span class="muted">待充装气瓶 ${pending.length} 只：</span>
      <button class="btn btn-primary" id="newBatch" ${isOps() ? "" : "disabled"}>新建充装批次</button></div></div>
    <div class="table-wrap"><table><thead><tr>
      <th>批次号</th><th>状态</th><th>接口数</th><th>创建人</th><th>创建时间</th><th>中断原因</th><th></th>
    </tr></thead><tbody>${batches.map(b => {
      const used = b.ports.filter(p => p.cyl_no).length;
      const stMap = { OPEN: "yellow", CHARGING: "yellow", SETTLING: "yellow", COMPLETED: "green", ABORTED: "red" };
      return `<tr>
        <td><b>${b.id}</b></td><td><span class="tag tag-${stMap[b.status]}">${b.status_label}</span></td>
        <td class="num">${used}/${b.ports.length}</td><td>${b.created_by}</td>
        <td>${new Date(b.created_ts * 1000).toLocaleString("zh-CN", { hour12: false })}</td>
        <td>${esc(b.abort_reason || "—")}</td>
        <td><button class="btn btn-sm" data-batch="${b.id}">查看接口</button></td></tr>`;
    }).join("")}</tbody></table></div>
    ${!isOps() ? `<p class="muted" style="margin-top:8px">当前角色为只读视图。</p>` : ""}`;
  $("#newBatch")?.addEventListener("click", () => guard(async () => {
    const r = await api("/api/batches", { method: "POST", body: { positions: 4 } });
    toast(`批次 ${r.batch_id} 已创建`, "ok");
    state.detailBatch = r.batch_id;
    render();
  }));
  $$("[data-batch]").forEach(b => b.addEventListener("click", () => {
    state.detailBatch = b.dataset.batch; render();
  }));
}

const PORT_STATUS = {
  WAITING: ["等待上瓶", "waiting"], FILLING: ["充装中", "filling"],
  FILLED_OK: ["静置待复测", "filling"], FILL_LOW: ["终压不足", "low"],
  RETEST_PASS: ["复测合格", "ok"], RETEST_LEAK: ["复测泄漏", "leak"],
};
async function viewBatchDetail(el, bid) {
  const b = await api(`/api/batches/${bid}`);
  const pending = state.cylinders.filter(c => c.status === "PENDING_FILL");
  const editable = b.status === "OPEN";
  const charging = b.status === "CHARGING";
  const settling = b.status === "SETTLING";
  el.innerHTML = `
    <div class="page-head"><h2>批次 ${b.id}
      <span class="tag tag-${b.status === "COMPLETED" ? "green" : b.status === "ABORTED" ? "red" : "yellow"}">${b.status_label}</span></h2>
      <div>
        <button class="btn btn-sm" id="backList">‹ 返回批次列表</button>
        ${editable ? `<button class="btn btn-yellow" id="bStart" ${isOps() ? "" : "disabled"}>开始充装</button>` : ""}
        ${charging ? `<button class="btn btn-yellow" id="bComplete" ${isOps() ? "" : "disabled"}>充装完成·读终压</button>` : ""}
        ${["OPEN", "CHARGING", "SETTLING"].includes(b.status) ? `<button class="btn btn-primary" id="bAbort" ${isOps() ? "" : "disabled"}>充装中断</button>` : ""}
      </div></div>
    ${b.abort_reason ? `<div class="warn-strip">中断原因：${esc(b.abort_reason)}（未完成瓶已退回待充装）</div>` : ""}
    <div class="muted" style="margin-bottom:12px">终压低于 ${state.thresholds.final_min} MPa 判终压不足并退出本批转维修；
      合格接口静置 ${Math.round(state.thresholds.settle_seconds / 60)} 分钟后复测。同一气瓶不可同时处于两个未结束批次。</div>
    <div class="bays">
      ${b.ports.map(p => {
        const [stLabel, stCls] = PORT_STATUS[p.status];
        return `<div class="bay is-${stCls}">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div class="bay-pos">${p.position}</div>
            <span class="tag tag-${stCls === "ok" ? "green" : stCls === "leak" || stCls === "low" ? "red" : "yellow"}">${stLabel}</span>
          </div>
          <div class="bay-cyl">${p.cyl_no || "〈空位〉"}</div>
          ${editable ? `
            <select data-assign="${p.position}">
              <option value="">— 选择待充装气瓶 —</option>
              ${pending.filter(c => c.cyl_no !== p.cyl_no).map(c =>
                `<option value="${c.cyl_no}">${c.cyl_no}（背架 ${c.frame_no}）</option>`).join("")}
            </select>
            ${p.cyl_no ? `<button class="btn btn-sm btn-ghost" data-unassign="${p.position}">移出 ${p.cyl_no}</button>` : ""}`
          : `<div class="bay-pressure">${p.final_pressure != null ? `${p.final_pressure}<small> MPa 终压</small>` : "—"}</div>
             ${p.retest_drop != null ? `<div class="muted">复测压降 ${p.retest_drop} MPa</div>` : ""}
             ${p.status === "FILLED_OK" ? `<div class="muted">静置中，见「压力复测」</div>` : ""}`}
        </div>`;
      }).join("")}
    </div>
    ${!isOps() ? `<p class="muted" style="margin-top:10px">当前角色为只读视图。</p>` : ""}`;
  $("#backList").onclick = () => { state.detailBatch = null; render(); };
  $("#bStart")?.addEventListener("click", () => guard(async () => {
    await api(`/api/batches/${bid}/start`, { method: "POST", body: {} });
    toast("充装开始", "ok"); render();
  }));
  $("#bComplete")?.addEventListener("click", () => guard(async () => {
    const r = await api(`/api/batches/${bid}/complete`, { method: "POST", body: {} });
    toast(r.low.length ? `完成：${r.low.length} 只终压不足转维修，${r.settling} 只进入静置`
      : `全部终压合格，${r.settling} 只进入静置`, r.low.length ? "err" : "ok");
    render();
  }));
  $("#bAbort")?.addEventListener("click", () => guard(async () => {
    const reason = prompt("中断原因（如停电、设备故障、紧急拉动）：", "充装中断");
    if (reason === null) return;
    const r = await api(`/api/batches/${bid}/abort`, { method: "POST", body: { reason } });
    toast(`批次已中断，退回 ${r.released.length} 只气瓶`, "err"); render();
  }));
  $$("[data-assign]").forEach(sel => sel.addEventListener("change", () => guard(async () => {
    if (!sel.value) return;
    await api(`/api/batches/${bid}/assign`, { method: "POST", body: {
      position: parseInt(sel.dataset.assign), cyl_no: sel.value } });
    toast(`${sel.value} 已上 ${sel.dataset.assign} 号接口`, "ok"); render();
  })));
  $$("[data-unassign]").forEach(btn => btn.addEventListener("click", () => guard(async () => {
    await api(`/api/batches/${bid}/unassign`, { method: "POST", body: {
      position: parseInt(btn.dataset.unassign) } });
    render();
  })));
}

/* ================= 5. 压力复测（大字号初值/终值） ================= */
async function viewRetests(el) {
  const rows = await api("/api/retests");
  el.innerHTML = `${meLine()}
    <div class="page-head"><h2>静置压力复测</h2>
      <div class="muted">初值=充装终压；静置后复测，压降 &gt; ${state.thresholds.settle_drop_max} MPa 判泄漏封存。大字号并排显示。</div></div>
    <div class="retest-grid">
      ${rows.map(r => {
        const done = r.status === "RETEST_PASS" || r.status === "RETEST_LEAK";
        const leak = r.status === "RETEST_LEAK";
        const pass = r.status === "RETEST_PASS";
        const mm = String(Math.floor(r.settle_remaining / 60)).padStart(2, "0");
        const ss = String(r.settle_remaining % 60).padStart(2, "0");
        return `<div class="retest-card ${leak ? "is-leak" : pass ? "is-pass" : "is-wait"}">
          <div class="retest-head">
            <strong>${r.cyl_no}</strong>
            <span class="tag tag-${leak ? "red" : pass ? "green" : "yellow"}">
              ${leak ? "复测泄漏·封存" : pass ? "复测合格" : r.due ? "可复测" : "静置中"}</span>
          </div>
          <div class="muted">批次 ${r.batch_id} · ${r.position} 号接口</div>
          <div class="retest-pair">
            <div class="rp-box"><div class="rp-label">静置初值</div>
              <div class="rp-val rp-initial">${r.retest_initial?.toFixed(1) ?? "—"}<small>MPa</small></div></div>
            <div class="rp-arrow">→</div>
            <div class="rp-box"><div class="rp-label">复测终值</div>
              <div class="rp-val ${leak ? "rp-final-bad" : done ? "rp-final-ok" : ""}">${r.retest_final?.toFixed(1) ?? "··"}<small>MPa</small></div></div>
          </div>
          ${done ? `<div class="rp-drop" style="color:${leak ? "var(--red)" : "var(--green)"}">
              压降 ${r.retest_drop} MPa ${leak ? "⚠ 超过阈值，封存待判" : "≤ 阈值，压力稳定"}</div>
             ${pass ? `<div class="muted" style="text-align:center">合格 → 前往「配套核验」核对背架编号后回库</div>` : ""}`
          : `<div style="text-align:center">
              ${r.due ? `<div class="countdown" style="color:var(--green)">静置期满，可复测</div>`
                : `剩余静置 <span class="countdown">${mm}:${ss}</span>`}
              <button class="btn btn-${r.due ? "yellow" : "sm btn-ghost"}" data-retest="${r.id}"
                style="margin-top:8px" ${isOps() ? "" : "disabled"}>${r.due ? "执行复测" : "强制复测"}</button>
            </div>`}
        </div>`;
      }).join("") || `<p class="muted">暂无充装记录</p>`}
    </div>`;
  $$("[data-retest]").forEach(b => b.addEventListener("click", () => guard(async () => {
    const r = await api(`/api/ports/${b.dataset.retest}/retest`,
      { method: "POST", body: { force: true } });
    toast(r.leak ? `复测泄漏：压降 ${r.drop} MPa，已封存待判`
      : `复测合格：压降 ${r.drop} MPa，压力稳定，可配套回库`, r.leak ? "err" : "ok");
    render();
  })));
}

/* ================= 6. 配套核验 ================= */
async function viewPairs(el) {
  const pairs = await api("/api/pairs");
  const cand = state.cylinders.filter(c =>
    ["PENDING_PAIR", "AVAILABLE", "PENDING_FILL"].includes(c.status));
  el.innerHTML = `${meLine()}
    <div class="grid-2" style="grid-template-columns:380px 1fr">
      <div class="card"><h2>背架重新配套核对</h2>
        <p class="muted">扫描背架编号并与气瓶登记背架核对；错误配套不放行。封存/报废瓶不可配套领用。</p>
        <div class="field"><label>气瓶</label><select id="pCyl">
          ${cand.map(c => `<option value="${c.cyl_no}">${c.cyl_no}（应配背架 ${c.frame_no}）</option>`).join("")}
        </select></div>
        <div class="field"><label>扫描背架编号</label><input id="pFrame" placeholder="如 JZ-0603"></div>
        <button class="btn btn-primary btn-block" id="pGo">核对配套</button>
        <hr style="border-color:var(--line);margin:14px 0">
        <div class="muted">提示：可故意输入 <b>JZ-9999</b> 体验错误配套拦截。</div>
      </div>
      <div class="card"><h2>核验记录</h2>
        <div class="table-wrap"><table><thead><tr>
          <th>时间</th><th>气瓶</th><th>登记背架</th><th>扫描背架</th><th>结果</th><th>操作人</th>
        </tr></thead><tbody>${pairs.map(p => `<tr>
          <td>${p.ts_str}</td><td><b>${p.cyl_no}</b></td>
          <td class="num">${p.frame_expected}</td>
          <td class="num" style="color:${p.matched ? "" : "#ff6b7e"}">${p.frame_scanned}</td>
          <td>${p.matched ? '<span class="tag tag-green">一致</span>' : '<span class="tag tag-red">错误配套</span>'}</td>
          <td>${esc(p.actor_name || p.actor_id)}</td></tr>`).join("")}</tbody></table></div>
      </div>
    </div>`;
  $("#pGo").addEventListener("click", () => guard(async () => {
    const frame = $("#pFrame").value.trim();
    if (!frame) return toast("请扫描/输入背架编号", "err");
    const r = await api("/api/pair-checks", { method: "POST", body: {
      cyl_no: $("#pCyl").value, frame_scanned: frame } });
    toast(r.returned_to_service ? "编号一致，气瓶回到可领用" : "编号一致（复核）", "ok");
    $("#pFrame").value = ""; render();
  }));
}

/* ================= 7. 检验到期 ================= */
async function viewDue(el) {
  const due = await api("/api/due");
  el.innerHTML = `${meLine()}
    <div class="page-head"><h2>气瓶检验到期视图</h2>
      <div class="muted">水压试验周期 ${state.thresholds.inspection_months} 个月；提前 ${30} 天黄色预警，超期红色停止流转。</div></div>
    ${due.some(d => d.level === "OVERDUE") ? `<div class="warn-strip">有 ${due.filter(d => d.level === "OVERDUE").length} 只气瓶检验超期，已禁止领用/入批；须由检验员更新检验记录。</div>` : ""}
    <div class="table-wrap"><table><thead><tr>
      <th>气瓶</th><th>背架</th><th>状态</th><th>上次检测</th><th>有效期至</th><th>剩余/超期</th><th>操作</th>
    </tr></thead><tbody>${due.map(c => `<tr>
      <td><b>${c.cyl_no}</b></td><td>${c.frame_no}</td><td>${tag(c.status)}</td>
      <td>${c.last_inspection}</td><td class="num">${c.inspection_due}</td>
      <td>${c.level === "OVERDUE"
        ? `<span class="tag tag-red">超期 ${-c.days_to_due} 天</span>`
        : `<span class="tag tag-yellow">${c.days_to_due} 天后到期</span>`}</td>
      <td>
        <button class="btn btn-sm" data-cyl="${c.cyl_no}" data-act="insp"
          ${can("INSPECTOR", "SUPERVISOR") ? "" : "disabled"}>登记检验</button>
        ${c.status === "SEALED" ? `<button class="btn btn-sm btn-yellow" data-cyl="${c.cyl_no}" data-act="unseal"
          ${isSupervisor() ? "" : "disabled"}>主管解封</button>` : ""}
      </td></tr>`).join("") || `<tr><td colspan="7">无临近/超期气瓶</td></tr>`}</tbody></table></div>
    ${!can("INSPECTOR", "SUPERVISOR") ? `<p class="muted" style="margin-top:8px">登记检验需检验员/主管权限；解除封存仅装备主管。</p>` : ""}`;
  $$("[data-act]").forEach(b => b.addEventListener("click", () => guard(async () => {
    const no = b.dataset.cyl;
    if (b.dataset.act === "insp") {
      const v = prompt(`为 ${no} 登记上次水压检验日期（YYYY-MM-DD）：`,
        new Date().toISOString().slice(0, 10));
      if (!v) return;
      const r = await api(`/api/cylinders/${no}/inspection`,
        { method: "POST", body: { last_inspection: v } });
      toast(`已更新，新有效期至 ${r.inspection_due}`, "ok");
    } else {
      const r = await api(`/api/cylinders/${no}/unseal`,
        { method: "POST", body: { reason: "检验合格，主管解除封存" } });
      toast("已解除封存", "ok");
    }
    render();
  })));
}

/* ================= 全程事件 ================= */
async function viewEvents(el) {
  const events = await api("/api/events?limit=300");
  el.innerHTML = `<div class="page-head"><h2>全程事件（SQLite 只追加）</h2>
    <div class="muted">共 ${events.length} 条，最近在前</div></div>
    <div class="card">${events.map(e => `<div class="event-line">
      <b>${e.type}</b> <span class="muted">${e.ts_str}</span>
      ${e.cyl_no ? `· 气瓶 <a href="#" data-cyl="${e.cyl_no}" style="color:var(--yellow)">${e.cyl_no}</a>` : ""}
      ${e.actor_id ? `· ${e.actor_id}` : ""}
      ${e.ref_type ? `· ${e.ref_type}#${e.ref_id}` : ""}
      ${e.payload ? `<br><span class="muted">${esc(e.payload)}</span>` : ""}
    </div>`).join("")}</div>`;
  $$("[data-cyl]", el).forEach(a => a.addEventListener("click", (e) => {
    e.preventDefault(); openCylDrawer(a.dataset.cyl);
  }));
}

boot().catch(e => toast("初始化失败：" + e.message, "err"));
