/* 消防 SCBA 前端（原生 JS，无构建依赖） */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const state = { view: "ledger", actor: null, meta: null, people: [], timer: null, noteSeq: 0 };

/* ---------------- HTTP ---------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: {
      "Content-Type": "application/json",
      ...(state.actor ? { "X-Actor-Id": state.actor } : {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}
const get = (p) => api(p);
const post = (p, body = {}) => api(p, { method: "POST", body });

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}
function toast(msg, kind = "") {
  const t = el(`<div class="toast ${kind}">${esc(msg)}</div>`);
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 4200);
}
function role() {
  return state.people.find((p) => p.id === state.actor)?.role;
}
function roleName() { return state.meta?.roles?.[role()] || ""; }

/* ---------------- 模态框 ---------------- */
function modal(title, bodyNode, footNode) {
  const card = $("#modalCard");
  card.innerHTML = "";
  card.appendChild(el(`<h3>${esc(title)}</h3>`));
  if (typeof bodyNode === "string") card.appendChild(el(bodyNode));
  else card.appendChild(bodyNode);
  if (footNode) card.appendChild(footNode);
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

function confirmModal(title, text, onOk, okClass = "btn-red") {
  const body = el(`<div><p>${esc(text)}</p><textarea id="cfReason" rows="2" style="width:100%"
      placeholder="备注/原因（可选）"></textarea></div>`);
  const foot = el(`<div class="modal-foot"></div>`);
  foot.appendChild(el(`<button class="btn-ghost">取消</button>`)).onclick = closeModal;
  const ok = el(`<button class="${okClass}">确认</button>`);
  ok.onclick = async () => {
    try { await onOk($("#cfReason", body).value); closeModal(); await refresh(); }
    catch (e) { toast(e.message, "error"); }
  };
  foot.appendChild(ok);
  modal(title, body, foot);
}

/* ---------------- 通用片段 ---------------- */
function statusBadge(c) {
  return `<span class="badge ${c.status}">${esc(c.status_name)}</span>`;
}
function pressureBar(p, rated) {
  const pct = Math.max(0, Math.min(120, (p / rated) * 100));
  const low = p < state.meta.fill_final_min;
  return `<div class="pbar ${low ? "low" : ""}"><i style="width:${pct}%"></i>
    <span class="pbar-label">${Number(p).toFixed(1)} / ${rated} MPa</span></div>`;
}
function dueClass(c) {
  return c.days_left < 0 ? "due-overdue" : c.days_left <= state.meta.warn_days ? "due-soon" : "due-ok";
}
function dueText(c) {
  if (c.days_left < 0) return `已超期 ${-c.days_left} 天`;
  if (c.days_left <= state.meta.warn_days) return `${c.days_left} 天后到期`;
  return `剩余 ${c.days_left} 天`;
}

/* ================= 视图 1：装备台账 ================= */
async function renderLedger() {
  const [cyls] = await Promise.all([get("/api/cylinders")]);
  const v = $("#view");
  v.innerHTML = "";
  const panel = el(`<div class="panel">
    <div class="row spread">
      <h2>气瓶装备台账 <span class="tag">编号 · 背架 · 压力 · 检验有效期 · 状态</span></h2>
      <div class="row">
        <input id="fq" placeholder="搜索气瓶/背架编号" style="min-width:190px"/>
        <select id="fstatus">
          <option value="">全部状态</option>
          ${Object.entries(state.meta.status_names).map(([k, n]) =>
            `<option value="${k}">${n}</option>`).join("")}
        </select>
      </div>
    </div>
  </div>`);
  const list = el(`<div class="cyl-list"></div>`);
  panel.appendChild(list);
  v.appendChild(panel);

  function draw(rows) {
    list.innerHTML = "";
    if (!rows.length) { list.appendChild(el(`<div class="muted" style="padding:14px">无匹配气瓶</div>`)); return; }
    for (const c of rows) {
      const actions = [];
      if (c.status === "available") actions.push(`<button class="btn-sm btn-yellow" data-act="issue">领用</button>`);
      if (c.status === "available" && role() === "supervisor")
        actions.push(`<button class="btn-sm" data-act="loan">紧急借调</button>`);
      if (c.status === "loaned")
        actions.push(`<button class="btn-sm btn-yellow" data-act="loanback">借调归还</button>`);
      if (c.status === "sealed" && role() === "supervisor")
        actions.push(`<button class="btn-sm" data-act="unseal">解除封存</button>`);
      if (c.status !== "scrapped" && role() === "supervisor")
        actions.push(`<button class="btn-sm btn-red" data-act="scrap">报废</button>`);
      const row = el(`<div class="cyl-row clickable">
        <div><div class="cyl-serial">${esc(c.serial_no)}</div>
             <div class="cyl-harness">背架 ${esc(c.harness_code || "未配套")}</div></div>
        <div>${statusBadge(c)}</div>
        <div>${pressureBar(c.current_pressure, c.rated_pressure)}
             <div class="pmeta">额定 <b>${c.rated_pressure} MPa</b></div></div>
        <div class="pmeta">检验有效期 <b class="${dueClass(c)}">${esc(c.inspection_due)}</b><br/>
             <span class="${dueClass(c)}">${dueText(c)}</span></div>
        <div class="cyl-actions">${actions.join("")}</div>
      </div>`);
      row.onclick = (e) => {
        if (e.target.dataset.act) return;
        showCylinder(c.id);
      };
      const actBtn = $(".cyl-actions", row);
      actBtn?.addEventListener("click", async (e) => {
        e.stopPropagation();
        const act = e.target.dataset.act;
        try {
          if (act === "issue") await issueDialog(c);
          if (act === "loan") await loanDialog(c);
          if (act === "loanback") await loanBackDialog(c);
          if (act === "unseal") await confirmModal("解除封存",
            `确认由装备主管解除 ${c.serial_no} 的封存？解除后转待检查。`,
            () => post(`/api/cylinders/${c.id}/unseal`), "btn-yellow");
          if (act === "scrap") await confirmModal("报废确认（仅装备主管）",
            `确认报废 ${c.serial_no}？该操作不可逆。`,
            (reason) => post(`/api/cylinders/${c.id}/scrap`, { reason }));
        } catch (err) { toast(err.message, "error"); }
      });
      list.appendChild(row);
    }
  }
  draw(cyls);
  let t;
  $("#fq", panel).oninput = () => { clearTimeout(t); t = setTimeout(apply, 250); };
  $("#fstatus", panel).onchange = apply;
  async function apply() {
    const q = $("#fq", panel).value.trim();
    const s = $("#fstatus", panel).value;
    draw(await get(`/api/cylinders?${s ? `status=${s}&` : ""}${q ? `q=${encodeURIComponent(q)}` : ""}`));
  }
}

async function showCylinder(id) {
  const d = await get(`/api/cylinders/${id}`);
  const body = el(`<div></div>`);
  body.innerHTML = `
    <div class="row" style="justify-content:space-between">
      <div><b style="font-size:18px">${esc(d.serial_no)}</b> ${statusBadge(d)}</div>
      <div class="muted">背架 ${esc(d.harness_code || "未配套")}</div>
    </div>
    <table class="tight" style="margin-top:10px">
      <tr><th>额定压力</th><td>${d.rated_pressure} MPa</td><th>当前压力</th><td>${d.current_pressure} MPa</td></tr>
      <tr><th>出厂日期</th><td>${esc(d.manufacture_date)}</td><th>上次检测</th><td>${esc(d.last_inspection_date)}</td></tr>
      <tr><th>检验到期</th><td class="${dueClass(d)}">${esc(d.inspection_due)}（${dueText(d)}）</td>
          <th>配套确认</th><td>${esc(d.paired_confirmed_at || "—")}</td></tr>
      ${d.seal_reason ? `<tr><th>封存原因</th><td colspan="3" style="color:#ff9aa6">${esc(d.seal_reason)}</td></tr>` : ""}
      ${d.scrap_reason ? `<tr><th>报废原因</th><td colspan="3" style="color:#ff9aa6">${esc(d.scrap_reason)}</td></tr>` : ""}
    </table>
    <h3 style="font-size:14px;margin:14px 0 6px">检测附件（本地留存）</h3>
    <div>${(d.attachments || []).map(a =>
      `<a class="btn btn-sm" style="margin:0 6px 6px 0" href="/api/attachments/${a.id}" target="_blank">
        📎 ${esc(a.filename)}<span class="muted"> · ${esc(a.note || a.kind)}</span></a>`).join("") ||
      '<span class="muted">无附件</span>'}</div>
    <h3 style="font-size:14px;margin:14px 0 6px">全程事件</h3>
    <div class="events">${(d.events || []).map(e =>
      `<div class="event-line"><time>${esc(e.ts)}</time><span><b>${esc(e.type)}</b> ${esc(e.detail)}</span></div>`).join("")}</div>`;
  const foot = el(`<div class="modal-foot"><button class="btn-ghost">关闭</button></div>`);
  foot.querySelector("button").onclick = closeModal;
  modal(`气瓶详情 ${esc(d.serial_no)}`, body, foot);
}

async function issueDialog(c) {
  const tasks = await get("/api/tasks");
  const fighters = state.people.filter((p) => p.role === "fighter");
  const body = el(`<div class="row" style="flex-direction:column;align-items:stretch;gap:12px">
    <label class="field">领用任务
      <select id="iTask">${tasks.filter(t => t.status !== "closed")
        .map(t => `<option value="${t.id}">${esc(t.code)} · ${esc(t.name)}</option>`).join("")}</select>
    </label>
    <label class="field">领用人
      <select id="iPerson">${fighters.map(f =>
        `<option value="${f.id}">${esc(f.name)}（${esc(f.badge)} ${esc(f.squad)}）</option>`).join("")}</select>
    </label>
    <div class="muted">发放压力 ${c.current_pressure} MPa。封存/报废瓶不可领用。</div>
  </div>`);
  const foot = el(`<div class="modal-foot"></div>`);
  foot.appendChild(el(`<button class="btn-ghost">取消</button>`)).onclick = closeModal;
  const ok = el(`<button class="btn-yellow">确认领用</button>`);
  ok.onclick = async () => {
    try {
      await post("/api/issue", {
        cylinder_id: c.id, task_id: Number($("#iTask", body).value),
        person_id: Number($("#iPerson", body).value),
      });
      closeModal(); toast(`${c.serial_no} 领用成功`, "success"); refresh();
    } catch (e) { toast(e.message, "error"); }
  };
  foot.appendChild(ok);
  modal("任务领用", body, foot);
}

async function loanDialog(c) {
  const body = el(`<div class="row" style="flex-direction:column;align-items:stretch;gap:12px">
    <label class="field">借入单位
      <input id="lUnit" value="特勤二站（跨队增援）" placeholder="如 特勤二站"/></label>
    <div class="muted">借调出站压力 ${c.current_pressure} MPa，期间不可被领用/组批（需装备主管批准）。</div>
  </div>`);
  const foot = el(`<div class="modal-foot"></div>`);
  foot.appendChild(el(`<button class="btn-ghost">取消</button>`)).onclick = closeModal;
  const ok = el(`<button class="btn-red">批准借调</button>`);
  ok.onclick = async () => {
    try {
      await post("/api/loans", { cylinder_id: c.id, borrower_unit: $("#lUnit", body).value });
      closeModal(); toast(`${c.serial_no} 已紧急借调出站`, "success"); refresh();
    } catch (e) { toast(e.message, "error"); }
  };
  foot.appendChild(ok);
  modal("紧急借调", body, foot);
}

async function loanBackDialog(c) {
  const body = el(`<div class="row" style="flex-direction:column;align-items:stretch;gap:12px">
    <label class="field">归还余压 MPa
      <input id="lbP" type="number" step="0.1" value="${Math.max(0, c.current_pressure - 2).toFixed(1)}"/></label>
    <div class="muted">归还后按归队流程进入待检查，外观合格后重新组批。</div>
  </div>`);
  const foot = el(`<div class="modal-foot"></div>`);
  foot.appendChild(el(`<button class="btn-ghost">取消</button>`)).onclick = closeModal;
  const ok = el(`<button class="btn-yellow">登记归还</button>`);
  ok.onclick = async () => {
    try {
      await post(`/api/cylinders/${c.id}/loan-return`, { pressure_back: Number($("#lbP", body).value) });
      closeModal(); toast(`${c.serial_no} 借调归还，进入待检查`, "success"); refresh();
    } catch (e) { toast(e.message, "error"); }
  };
  foot.appendChild(ok);
  modal("紧急借调归还", body, foot);
}

/* ================= 视图 2：任务领用 ================= */
async function renderTasks() {
  const tasks = await get("/api/tasks");
  const v = $("#view");
  v.innerHTML = `<div class="panel"><h2>训练任务与领用 <span class="tag">人员与任务来自训练数据</span></h2></div>`;
  const grid = el(`<div class="card-grid"></div>`);
  for (const t of tasks) {
    const card = el(`<div class="task-card">
      <h3>${esc(t.name)} <span class="badge ${t.status === "ongoing" ? "filling" :
        t.status === "closed" ? "scrapped" : "issued"}">${{ planned: "计划", ongoing: "进行中", closed: "已结束" }[t.status]}</span></h3>
      <div class="muted">${esc(t.code)} · ${esc(t.kind)} · ${esc(t.location)} · ${esc(t.scheduled_at)}</div>
      <div id="asgs"></div>
    </div>`);
    const box = $("#asgs", card);
    if (!t.assignments.length) box.appendChild(el(`<div class="muted" style="margin-top:8px">暂无领用</div>`));
    for (const a of t.assignments) {
      box.appendChild(el(`<div class="asg">
        <span><b>${esc(a.serial_no)}</b> · ${esc(a.person_name)}
          <span class="badge ${a.status === "issued" ? "issued" : "ok"}">${a.status === "issued" ? "出勤中" : "已归队"}</span></span>
        <span class="muted">${Number(a.issued_pressure).toFixed(1)} → ${a.return_pressure != null ? Number(a.return_pressure).toFixed(1) : "—"} MPa</span>
      </div>`));
    }
    grid.appendChild(card);
  }
  v.appendChild(grid);

  const quick = el(`<div class="panel" style="margin-top:16px">
    <h2>快捷归队登记</h2>
    <div class="row">
      <select id="rcCyl" style="min-width:200px"></select>
      <label class="field">归队余压 MPa
        <input id="rcP" type="number" step="0.1" style="width:130px" placeholder="如 9.5"/></label>
      <input id="rcNote" placeholder="备注（可选）" style="flex:1;min-width:160px"/>
      <button class="btn-red" id="rcBtn" style="margin-top:14px">扫码归队</button>
    </div>
    <div class="muted" id="rcHint" style="margin-top:8px"></div>
  </div>`);
  const cyls = await get("/api/cylinders");
  const issued = cyls.filter(c => c.status === "issued");
  $("#rcCyl", quick).innerHTML = (issued.length ? issued : cyls).map(c =>
    `<option value="${c.id}">${esc(c.serial_no)}（${c.status_name}）</option>`).join("");
  $("#rcBtn", quick).onclick = async () => {
    try {
      const r = await post("/api/returns", {
        cylinder_id: Number($("#rcCyl", quick).value),
        return_pressure: Number($("#rcP", quick).value),
        note: $("#rcNote", quick).value,
      });
      if (r.dedup) {
        toast(`重复扫描：该瓶已有待检查归队记录（累计扫描 ${r.dup_scan_count} 次，仅保留一条）`, "");
      } else {
        toast(r.low_pressure ? "归队成功：余压偏低，请关注" : "归队成功，进入待检查", "success");
      }
      refresh();
    } catch (e) { toast(e.message, "error"); }
  };
  v.appendChild(quick);
}

/* ================= 视图 3：归队检查 ================= */
async function renderReturns() {
  const rows = await get("/api/returns");
  const v = $("#view");
  v.innerHTML = `<div class="panel"><h2>归队检查 <span class="tag">外观合格且检验在有效期内方可加入充装批次</span></h2></div>`;
  const grid = el(`<div class="card-grid"></div>`);
  const pending = rows.filter(r => r.status === "pending");
  if (!pending.length) v.appendChild(el(`<div class="muted">当前没有待检查气瓶</div>`));
  for (const r of pending) {
    const low = r.low_pressure;
    const card = el(`<div class="ret-card">
      <h3>${esc(r.serial_no)} ${low ? `<span class="badge sealed">余压偏低</span>` : ""}
        ${r.dup_scan_count ? `<span class="badge filling">重复扫码 ×${r.dup_scan_count + 1}</span>` : ""}</h3>
      <div class="muted">归队余压 <b style="color:${low ? "#ff808d" : "#fff"}">${r.return_pressure} MPa</b>
        · 背架 ${esc(r.harness_code || "未配套")} · 归队人 ${esc(r.returned_by_name || "—")}</div>
      <div class="muted">检验到期 ${esc(r.inspection_due)}
        <b class="${r.days_left < 0 ? "due-overdue" : r.days_left <= state.meta.warn_days ? "due-soon" : "due-ok"}">
          （${r.days_left < 0 ? `已超期 ${-r.days_left} 天` : `剩余 ${r.days_left} 天`}）</b></div>
      <div style="margin-top:8px">
        <label class="checkline"><input type="checkbox" class="ck" checked/> 瓶体外观合格（无变形/腐蚀/划伤）</label>
        <label class="checkline"><input type="checkbox" class="ckv" checked/> 瓶阀完好（无损伤、启闭正常）</label>
        <label class="checkline"><input type="checkbox" class="ckl" checked/> 标识/标签清晰完好</label>
      </div>
      <textarea class="find" rows="2" style="width:100%;margin-top:8px" placeholder="检查发现/备注"></textarea>
      <div class="row" style="margin-top:10px">
        <button class="btn-yellow pass">检查通过（入待充装池）</button>
        <button class="btn-red seal">封存</button>
      </div>
    </div>`);
    $(".pass", card).onclick = async () => submitInspect(r, card, false);
    $(".seal", card).onclick = async () => submitInspect(r, card, true);
    grid.appendChild(card);
  }
  v.appendChild(grid);

  const done = rows.filter(r => r.status !== "pending").slice(0, 8);
  if (done.length) {
    const hist = el(`<div class="panel" style="margin-top:16px"><h2>近期检查记录</h2>
      <table class="tight"><thead><tr><th>气瓶</th><th>余压</th><th>外观</th><th>瓶阀</th><th>结论</th><th>发现</th></tr></thead><tbody>
      ${done.map(r => {
        const i = r.latest_inspection;
        const conclusion = i ? (i.valve_ok ? (i.appearance_ok && i.label_ok
          ? (r.days_left < 0 ? "超期封存" : "通过") : "缺陷封存") : "瓶阀损伤封存") : "—";
        return `<tr><td>${esc(r.serial_no)}</td><td>${r.return_pressure}</td>
          <td>${i ? (i.appearance_ok ? "✅" : "❌") : "—"}</td>
          <td>${i ? (i.valve_ok ? "✅" : "❌") : "—"}</td>
          <td>${conclusion}</td><td>${esc(i?.findings || "")}</td></tr>`;
      }).join("")}</tbody></table></div>`);
    v.appendChild(hist);
  }
}

async function submitInspect(r, card, forceSeal) {
  try {
    const payload = {
      return_id: r.id,
      appearance_ok: forceSeal ? false : $(".ck", card).checked,
      valve_ok: forceSeal ? false : $(".ckv", card).checked,
      label_ok: forceSeal ? true : $(".ckl", card).checked,
      findings: $(".find", card).value || (forceSeal ? "检查封存" : ""),
    };
    const res = await post("/api/inspections", payload);
    const map = { pass: "外观检查通过，可加入充装批次", valve_damage: "瓶阀损伤，已封存",
      appearance_defect: "外观缺陷，已封存", overdue: `超期未检 ${-res.days_left} 天，已封存` };
    toast(map[res.result] || "检查完成", res.result === "pass" ? "success" : "error");
    refresh();
  } catch (e) { toast(e.message, "error"); }
}

/* ================= 视图 4：充装组批 ================= */
async function renderBatches() {
  const [batches, cyls] = await Promise.all([get("/api/batches?active=1"), get("/api/cylinders")]);
  const v = $("#view");
  v.innerHTML = "";
  const ctrl = el(`<div class="panel"><div class="row spread">
    <h2>充装批次 <span class="tag">同一气瓶不能进入两个未结束批次 · 每批 ${state.meta.ports} 个接口</span></h2>
    <button class="btn-red" id="newBatch">新建充装批次</button></div></div>`);
  ctrl.querySelector("#newBatch").onclick = async () => {
    try { await post("/api/batches", {}); toast("已新建批次", "success"); refresh(); }
    catch (e) { toast(e.message, "error"); }
  };
  v.appendChild(ctrl);

  // 入批控制
  const eligible = cyls.filter(c => c.status === "returned_pending");
  const enq = el(`<div class="panel"><h2>待充装气瓶组批</h2>
    <div class="row">
      <select id="qCyl" style="min-width:200px">${eligible.map(c =>
        `<option value="${c.id}">${esc(c.serial_no)} · ${Number(c.current_pressure).toFixed(1)} MPa</option>`).join("")
        || `<option value="">（无待充装气瓶）</option>`}</select>
      <select id="qBatch">${batches.filter(b => b.status === "open" || b.status === "running")
        .map(b => `<option value="${b.id}">${esc(b.code)}（${{ open: "组批中", running: "充装中" }[b.status]}）</option>`).join("")
        || `<option value="">（无开放批次，请新建）</option>`}</select>
      <select id="qScenario" title="模拟压力台场景">
        <option value="">压力台：正常</option>
        <option value="leak">模拟：静置泄漏</option>
        <option value="low">模拟：终压不足</option>
      </select>
      <button class="btn-yellow" id="qBtn">加入下一空接口</button>
    </div>
    <div class="muted" style="margin-top:8px">故障注入由模拟压力台适配器提供，用于演示终压不足/复测泄漏处置。</div>
  </div>`);
  enq.querySelector("#qBtn").onclick = async () => {
    const bid = $("#qBatch", enq).value, cid = $("#qCyl", enq).value;
    if (!bid || !cid) return toast("缺少批次或气瓶", "error");
    try {
      await post(`/api/batches/${bid}/enqueue`, {
        cylinder_id: Number(cid), scenario: $("#qScenario", enq).value,
      });
      toast("已加入接口位", "success"); refresh();
    } catch (e) { toast(e.message, "error"); }
  };
  v.appendChild(enq);

  for (const b of batches) {
    const panel = el(`<div class="panel">
      <div class="row spread">
        <h2>${esc(b.code)} <span class="badge ${b.status}">${{
          open: "组批中", running: "充装中", interrupted: "已中断", done: "待复测/复测中", closed: "已结束" }[b.status]}</span></h2>
        <div class="row">
          ${b.status === "open" ? `<button class="btn-yellow" data-a="start">开始充装</button>` : ""}
          ${b.status === "running" ? `<button class="btn-yellow" data-a="complete">充装完成（读终压）</button>` : ""}
          ${["open", "running"].includes(b.status) ? `<button class="btn-red" data-a="interrupt">充装中断</button>` : ""}
        </div>
      </div>
      <div class="ports" style="margin-top:12px"></div>
    </div>`);
    const ports = $(".ports", panel);
    const byPos = Object.fromEntries(b.positions.map(p => [p.position, p]));
    for (let pos = 1; pos <= state.meta.ports; pos++) {
      const p = byPos[pos];
      const node = p ? filledPort(p, b) : emptyPort(pos);
      ports.appendChild(node);
    }
    $$("button[data-a]", panel).forEach(btn => btn.onclick = async () => {
      const a = btn.dataset.a;
      try {
        if (a === "start") { await post(`/api/batches/${b.id}/start`, {}); toast("充装开始", "success"); }
        if (a === "complete") {
          const r = await post(`/api/batches/${b.id}/complete`, {});
          if (r.low_final.length) toast(`终压不足已封存：${r.low_final.join("、")}`, "error");
          else toast("充装完成，等待静置复测", "success");
        }
        if (a === "interrupt") await confirmModal("充装中断",
          "中断后在充气瓶全部退出并恢复待检查，可重新组批。填写中断原因：",
          (reason) => post(`/api/batches/${b.id}/interrupt`, { reason: reason || "现场中断" }), "btn-red");
        refresh();
      } catch (e) { toast(e.message, "error"); }
    });
    v.appendChild(panel);
  }
  if (!batches.length) v.appendChild(el(`<div class="muted">暂无进行中的批次</div>`));
}

const PORT_STATE = { queued: "排队", filling: "充装中", filled: "待复测", leak: "泄漏封存",
  low_pressure: "终压不足封存", interrupted: "已中断" };

function emptyPort(pos) {
  return el(`<div class="port s-queued"><div class="port-head"><span class="port-num">接口 ${pos}</span>
    <span class="port-state muted">空闲</span></div>
    <div class="port-body"><div class="port-empty">空位<br/><span class="small">等待气瓶上架</span></div></div></div>`);
}
function filledPort(p, b) {
  const node = el(`<div class="port s-${p.position_state}">
    <div class="port-head"><span class="port-num">接口 ${p.position}</span>
      <span class="port-state">${PORT_STATE[p.position_state] || p.position_state}
        ${p.scenario === "leak" ? " · 泄漏模拟" : p.scenario === "low" ? " · 欠压模拟" : ""}</span></div>
    <div class="port-body">
      <div><b style="font-size:16px">${esc(p.serial_no)}</b></div>
      <div class="port-pressure">${p.fill_final != null
        ? `终压 <b>${Number(p.fill_final).toFixed(1)} MPa</b>`
        : p.position_state === "filling" ? "压力台充注中…" : "等待充装"}</div>
      <div class="muted small">入口初读 ${p.retest_initial != null ? Number(p.retest_initial).toFixed(1) : "—"} MPa</div>
      ${p.position_state === "filled"
        ? `<div class="countdown small" data-due="${p.retest_due_at}">静置复测计时…</div>` : ""}
      ${p.leak_result ? `<div class="badge ${p.leak_result === "pass" ? "ok" : "sealed"}">
        ${p.leak_result === "pass" ? "复测合格" : "复测泄漏"}</div>` : ""}
    </div></div>`);
  return node;
}

/* ================= 视图 5：压力复测 ================= */
async function renderRetest() {
  const data = await get("/api/retest");
  const v = $("#view");
  v.innerHTML = `<div class="panel"><h2>静置压力复测 <span class="tag">
    静置约 ${state.meta.settle_seconds} 秒 · 压降阈值 ${state.meta.drop_limit} MPa · 稳定方可回可领用</span></h2></div>`;

  const due = el(`<div class="panel"><h2>待复测（${data.due.length}）</h2>
    <div class="card-grid" id="dueGrid"></div></div>`);
  const grid = $("#dueGrid", due);
  if (!data.due.length) grid.appendChild(el(`<div class="muted">暂无到期复测；充装完成后由进程内任务自动生成。</div>`));
  for (const d of data.due) {
    grid.appendChild(el(`<div class="port s-filled">
      <div class="port-head"><span class="port-num">${esc(d.serial_no)}</span>
        <span class="port-state">${esc(d.batch_code)} · 接口 ${d.position}</span></div>
      <div class="port-body">
        <div class="duo">
          <div class="val-box"><div class="cap">初值（充完）</div>
            <div class="val init">${Number(d.fill_final).toFixed(1)}</div></div>
          <div class="arrow">→</div>
          <div class="val-box"><div class="cap">终值（复测）</div>
            <div class="val fin">--.-</div></div>
        </div>
        <div class="countdown small" data-due="${d.retest_due_at}">静置复测计时…</div>
        <button class="btn-yellow" data-pid="${d.position_id}">立即复测（读压力台）</button>
      </div></div>`));
  }
  $$("button[data-pid]", due).forEach(btn => btn.onclick = async () => {
    btn.disabled = true;
    try {
      const r = await post("/api/retest", { position_id: Number(btn.dataset.pid) });
      toast(r.leak ? `复测泄漏，压降 ${r.drop} MPa，已封存` : `复测合格，终压 ${r.final} MPa，恢复可领用`,
        r.leak ? "leak" : "success");
      refresh();
    } catch (e) { toast(e.message, "error"); btn.disabled = false; }
  });
  v.appendChild(due);

  const done = el(`<div class="panel"><h2>复测记录（初值 / 终值并排）</h2>
    <div class="card-grid" id="doneGrid"></div></div>`);
  const dg = $("#doneGrid", done);
  if (!data.done.length) dg.appendChild(el(`<div class="muted">暂无复测记录</div>`));
  for (const d of data.done) {
    const leak = d.leak_result === "leak";
    dg.appendChild(el(`<div class="port s-${leak ? "leak" : "filled"}">
      <div class="port-head"><span class="port-num">${esc(d.serial_no)}</span>
        <span class="port-state">${esc(d.batch_code)}</span></div>
      <div class="port-body">
        <div class="duo">
          <div class="val-box"><div class="cap">初值 MPa</div>
            <div class="val init">${Number(d.fill_final).toFixed(1)}</div></div>
          <div class="arrow">→</div>
          <div class="val-box"><div class="cap">终值 MPa</div>
            <div class="val fin ${leak ? "leakv" : ""}">${Number(d.retest_final).toFixed(1)}</div></div>
        </div>
        <div class="drop-tag ${leak ? "bad" : "good"}">
          压降 ${Number(d.pressure_drop).toFixed(2)} MPa · ${leak
            ? `超过 ${state.meta.drop_limit}，判泄漏封存` : "压力稳定，合格回库"}</div>
      </div></div>`));
  }
  v.appendChild(done);
}

/* ================= 视图 6：配套核验 ================= */
async function renderPairing() {
  const [data, harnesses] = await Promise.all([get("/api/pairing"), get("/api/harnesses")]);
  const v = $("#view");
  v.innerHTML = `<div class="panel"><h2>配套核验 <span class="tag">气瓶与背架重新配套必须核对编号；封存/报废瓶不可配套</span></h2>
    <div class="row">
      <select id="pCyl" style="min-width:200px">${data.rows.map(r =>
        `<option value="${r.id}">${esc(r.serial_no)}（当前 ${esc(r.harness_code || "未配套")}）</option>`).join("")}</select>
      <input id="pScan" placeholder="扫描/输入背架编号（如 BJ-A01）" style="min-width:220px"/>
      <button class="btn-yellow" id="pBtn">核对编号并配套</button>
      ${role() === "supervisor" ? `<button class="btn-red" id="pForce">主管强制改套</button>` :
        `<span class="muted">强制改套仅装备主管可操作</span>`}
    </div></div>`;

  async function doPair(force) {
    try {
      const cid = Number($("#pCyl").value), code = $("#pScan").value.trim();
      if (!code) return toast("请输入背架编号", "error");
      if (force) await post("/api/repaint", { cylinder_id: cid, harness_code: code });
      else await post("/api/pair", { cylinder_id: cid, harness_code: code });
      toast("编号核对一致，配套完成", "success"); refresh();
    } catch (e) { toast(e.message, "error"); }
  }
  $("#pBtn").onclick = () => doPair(false);
  $("#pForce")?.addEventListener("click", () => doPair(true));

  const list = el(`<div class="cyl-list"></div>`);
  for (const r of data.rows) {
    list.appendChild(el(`<div class="cyl-row">
      <div><div class="cyl-serial">${esc(r.serial_no)}</div>
        <div class="cyl-harness">${esc(state.meta.status_names[r.status])}</div></div>
      <div><span class="badge ${r.status}">${state.meta.status_names[r.status]}</span></div>
      <div class="pmeta">背架编号 <b>${esc(r.harness_code || "未配套")}</b>
        <div class="small">${esc(r.harness_model || "")}</div></div>
      <div class="pmeta">最近配套确认<br/><b>${esc(r.paired_confirmed_at || "—")}</b></div>
      <div></div>
    </div>`));
  }
  v.appendChild(el(`<div class="panel"><h2>配套台账</h2></div>`));
  v.lastChild.appendChild(list);
}

/* ================= 视图 7：检验到期 ================= */
async function renderExpiry() {
  const d = await get("/api/expiry");
  const v = $("#view");
  v.innerHTML = `<div class="panel"><h2>检验到期视图 <span class="tag">提前 ${d.warn_days} 天提醒 · 进程内任务自动刷新</span></h2>
    <div class="row" style="gap:24px">
      <div class="exp-head"><span class="exp-num" style="color:#ff6b7a">${d.overdue.length}</span><span class="muted">已超期</span></div>
      <div class="exp-head"><span class="exp-num" style="color:var(--yellow)">${d.soon.length}</span><span class="muted">${d.warn_days} 天内到期</span></div>
    </div></div>`;
  const cols = el(`<div class="duo-cols"></div>`);
  function col(title, rows, bad) {
    const c = el(`<div class="panel"><h2>${title}（${rows.length}）</h2><div class="cyl-list"></div></div>`);
    const box = $(".cyl-list", c);
    if (!rows.length) box.appendChild(el(`<div class="muted">无</div>`));
    for (const x of rows) {
      box.appendChild(el(`<div class="cyl-row">
        <div><div class="cyl-serial">${esc(x.serial_no)}</div>
          <div class="cyl-harness">背架 ${esc(x.harness_code || "未配套")}</div></div>
        <div>${statusBadge(x)}</div>
        <div>${pressureBar(x.current_pressure, x.rated_pressure)}</div>
        <div class="pmeta">到期 <b>${esc(x.inspection_due)}</b><br/>
          <span class="${bad ? "due-overdue" : "due-soon"}">${bad ? `超期 ${-x.days_left} 天` : `还剩 ${x.days_left} 天`}</span></div>
        <div class="cyl-actions">${x.status === "sealed" && role() === "supervisor"
          ? `<button class="btn-sm" data-u="${x.id}">解除封存</button>` : ""}</div>
      </div>`));
    }
    $$("button[data-u]", c).forEach(b => b.onclick = async () => {
      try { await post(`/api/cylinders/${b.dataset.u}/unseal`, {}); toast("已解除封存，转待检查", "success"); refresh(); }
      catch (e) { toast(e.message, "error"); }
    });
    return c;
  }
  cols.appendChild(col("已超期（须送检）", d.overdue, true));
  cols.appendChild(col("即将到期", d.soon, false));
  v.appendChild(cols);
}

/* ---------------- 倒计时刷新 ---------------- */
function tickCountdowns() {
  const now = new Date();
  $$("[data-due]").forEach((n) => {
    const due = new Date(n.dataset.due.replace(" ", "T"));
    const s = Math.max(0, Math.round((due - now) / 1000));
    if (s <= 0) n.textContent = "复测已到期，后台自动执行…";
    else {
      const mm = String(Math.floor(s / 60)).padStart(2, "0");
      const ss = String(s % 60).padStart(2, "0");
      n.textContent = `静置中 ${mm}:${ss} 后复测`;
    }
  });
}

/* ---------------- 通知轮询 ---------------- */
async function pollNotifications() {
  try {
    const d = await get(`/api/notifications?after=${state.noteSeq}`);
    if (d.items.length) {
      state.noteSeq += d.items.length;
      let unread = 0;
      for (const n of d.items) {
        unread++;
        toast(n.message, n.kind === "leak" ? "leak" : n.kind.includes("error") ? "error" : "success");
      }
      const bell = $("#notifyCount");
      bell.textContent = Number(bell.textContent || 0) + unread;
      bell.classList.remove("hidden");
      // 数据被后台任务改变，刷新当前视图
      refresh();
    }
  } catch { /* 忽略轮询错误 */ }
}

/* ---------------- 引导 ---------------- */
const VIEWS = { ledger: renderLedger, tasks: renderTasks, returns: renderReturns,
  batches: renderBatches, retest: renderRetest, pairing: renderPairing, expiry: renderExpiry };

async function refresh() {
  if (!state.meta) return;
  await VIEWS[state.view]();
  tickCountdowns();
}

async function boot() {
  [state.meta, state.people] = await Promise.all([get("/api/meta"), get("/api/people")]);
  const sel = $("#actorSelect");
  sel.innerHTML = state.people.map(p =>
    `<option value="${p.id}">${esc(p.name)} · ${esc(state.meta.roles[p.role])}（${esc(p.squad)}）</option>`).join("");
  // 默认选中充装操作员，便于演示
  sel.value = state.people.find(p => p.role === "operator")?.id || state.people[0].id;
  state.actor = Number(sel.value);
  sel.onchange = () => { state.actor = Number(sel.value); refresh(); };

  $("#tabs").addEventListener("click", (e) => {
    if (!e.target.dataset.view) return;
    state.view = e.target.dataset.view;
    $$(".tab").forEach(t => t.classList.toggle("active", t === e.target));
    refresh();
  });
  $("#notifyBell").onclick = () => { $("#notifyCount").classList.add("hidden"); $("#notifyCount").textContent = "0"; };

  await refresh();
  setInterval(tickCountdowns, 1000);
  setInterval(pollNotifications, 3000);
  // 定时刷新反映后台复测/到期状态
  setInterval(() => { if (["batches", "retest"].includes(state.view)) refresh(); }, 5000);
}
boot().catch(e => toast("启动失败：" + e.message, "error"));
