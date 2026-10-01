/* 内部工作台前端逻辑：原生 JS，无构建依赖。 */
"use strict";

const state = {
  meta: { stores: [], agents: [], active_rule: null, walkin: "WALKIN" },
  actor: "",
  tab: "properties",
  props: [], customers: [],
  selectedProp: null, selectedCust: null,
  cal: new Date(2026, 9, 1), // 种子数据以 2026-10 为当前月
  calData: null,
  deals: [], selectedDeal: null,
  signContext: null, // {viewing_id}
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const yuan = (cents) => "¥" + (Number(cents || 0) / 100).toLocaleString("zh-CN",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (p) => (Math.round(p * 100) / 100) + "%";

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = new Error(data.error || `HTTP ${res.status}`);
    e.conflict = !!data.conflict;
    throw e;
  }
  return data;
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  t.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { t.hidden = true; }, 3200);
}

function agentName(id) {
  if (id === state.meta.walkin) return "客户自行到店（门店公盘）";
  const a = state.meta.agents.find((x) => x.agent_id === id);
  return a ? a.name : id;
}
function storeName(id) {
  const s = state.meta.stores.find((x) => x.store_id === id);
  return s ? s.name : id;
}
function statusTag(status) {
  const map = {
    scheduled: ["待签到", "warn"], checked: ["已签到", "ok"], cancelled: ["已取消", "danger"],
    "系统建议": ["系统建议归属", "ok"], "待裁决": ["待主管裁决", "copper"],
    "保护期外": ["保护期外成交", "gray"], "已裁决": ["已主管裁决", "ok"],
  };
  const [text, cls] = map[status] || [status, ""];
  return `<span class="tag ${cls}">${esc(text)}</span>`;
}

// ------------------------------------------------------------------ init
async function init() {
  await loadMeta();
  bindTabs();
  bindGlobal();
  bindProperties();
  bindCustomers();
  bindCalendar();
  bindDisputes();
  bindSignModal();
  await loadProperties();
  await loadCustomers();
  await loadCalendar();
  await loadDeals();
  await loadAudit();
}

async function loadMeta() {
  state.meta = await api("GET", "/api/meta");
  const rule = state.meta.active_rule;
  $("#ruleChip").textContent = rule
    ? `规则版本 ${rule.version} · 保护期 ${rule.body.protection_days} 天 · 首带 ${rule.body.first_view_share}% / 末带 ${rule.body.last_view_share}%`
    : "规则版本 —（尚未签署）";
  const sel = $("#actorSelect");
  sel.innerHTML = state.meta.agents
    .map((a) => `<option value="${a.agent_id}">${esc(a.name)}（${esc(storeName(a.store_id))}·${a.role === "supervisor" ? "主管" : "经纪人"}）</option>`)
    .join("") + `<option value="${state.meta.walkin}">到店登记台（无归属）</option>`;
  const sup = state.meta.agents.find((a) => a.role === "supervisor");
  state.actor = sup ? sup.agent_id : state.meta.agents[0]?.agent_id || "";
  sel.value = state.actor;
  sel.onchange = () => { state.actor = sel.value; };
}

function bindTabs() {
  $("#tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    const name = btn.dataset.tab;
    state.tab = name;
    $$(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + name));
    if (name === "calendar") loadCalendar();
    if (name === "disputes") loadDeals();
    if (name === "audit") loadAudit();
  });
}

function bindGlobal() {}

// ------------------------------------------------------------------ 房源
function bindProperties() {
  $("#propSearchBtn").onclick = loadProperties;
  $("#propQ").addEventListener("keydown", (e) => e.key === "Enter" && loadProperties());
}

async function loadProperties() {
  state.props = await api("GET", "/api/properties?q=" + encodeURIComponent($("#propQ").value));
  $("#propList").innerHTML = state.props.map((p) => `
    <li data-id="${p.property_id}" class="${state.selectedProp === p.property_id ? "selected" : ""}">
      <div class="l1">${esc(p.address)}</div>
      <div class="l2">${esc(p.layout || "—")} · ${p.area_sqm || "—"}㎡ · ${yuan(p.price_cents)}</div>
      <div class="tags"><span class="tag gray">${esc(p.store_name)}</span>${p.status === "在售" ? '<span class="tag ok">在售</span>' : `<span class="tag gray">${esc(p.status)}</span>`}</div>
    </li>`).join("") || '<li class="empty" style="cursor:default">无匹配房源</li>';
  $$("#propList li[data-id]").forEach((li) =>
    li.onclick = () => showProperty(li.dataset.id));
}

async function showProperty(pid) {
  state.selectedProp = pid;
  loadProperties();
  const p = await api("GET", "/api/properties/" + pid);
  $("#propDetail").innerHTML = renderPropertyDetail(p);
  wirePropertyActions(p);
}

function protectionBox(v) {
  const pw = v.protection;
  if (!pw) return "";
  const cls = pw.expired ? "protect-box expired" : "protect-box";
  let line;
  if (pw.expired) line = `保护期已于 ${pw.end} 结束（${pw.days} 天口径，规则冻结于该带看版本）`;
  else if (pw.active) line = `保护期中：${pw.begin} 至 ${pw.end}，剩余 ${pw.remaining} 天`;
  else line = `保护期自 ${pw.begin} 起至 ${pw.end}（尚未起算）`;
  return `<div class="${cls}">${line}${pw.customer_confirmed ? ' · <b>客户已确认</b>' : ' · <span class="tag danger">客户未确认</span>'}</div>`;
}

function evidenceLine(v) {
  const sign = v.sign_path
    ? `<a href="${v.sign_path}" target="_blank"><img src="${v.sign_path}" alt="签字图"></a>`
    : '<span class="tag danger">缺签字</span>';
  const confirm = v.confirmed_at
    ? `<span class="tag ok">客户确认 ${esc(v.confirmed_at)}</span>`
    : `<span class="tag warn">客户未确认 · 码 ${v.confirm_code}</span>`;
  return `<div class="tl-evidence">${sign}<span class="tag ${v.status === "checked" ? "ok" : "danger"}">${v.status === "checked" ? "签到 " + esc(v.checked_in_at) : esc(v.status)}</span>${confirm}${v.collab ? '<span class="tag copper">跨店协作</span>' : ""}</div>`;
}

function renderPropertyDetail(p) {
  const agentOpts = state.meta.agents
    .map((a) => `<option value="${a.agent_id}">${esc(a.name)}（${esc(storeName(a.store_id))}）</option>`)
    .join("") + `<option value="${state.meta.walkin}">客户自行到店（登记台）</option>`;
  const custOpts = state.customers.length
    ? state.customers.map((c) => `<option value="${c.customer_id}">${esc(c.name)} · ${esc(c.phone)}</option>`).join("")
    : "";
  const tl = p.timeline.map((v) => `
    <li class="kind-${v.status}">
      <span class="tl-time">${esc(v.appointment_time)}</span>
      <div class="tl-body">${esc(agentName(v.agent_id))} 带看客户「${esc(v.customer.name)}」· ${esc(v.store_name)}</div>
      ${protectionBox(v)}
      ${evidenceLine(v)}
      ${v.status === "cancelled" ? `<div class="muted">取消原因：${esc(v.cancel_reason || "—")}（${esc(v.cancelled_at)}）</div>` : ""}
      <div class="muted">带看冻结规则版本：${esc(v.rule_version)}</div>
    </li>`).join("") || '<li class="empty">暂无带看安排</li>';
  return `
    <div class="panel">
      <div class="detail-head">
        <h2>${esc(p.address)}</h2>
        <div class="addr-line">${esc(p.store_name)} 房源 · ${esc(p.property_id)}</div>
        <div class="facts">
          <div class="fact"><div class="k">报价</div><div class="v price">${yuan(p.price_cents)}</div></div>
          <div class="fact"><div class="k">户型</div><div class="v">${esc(p.layout || "—")}</div></div>
          <div class="fact"><div class="k">面积</div><div class="v">${p.area_sqm || "—"} ㎡</div></div>
          <div class="fact"><div class="k">状态</div><div class="v">${esc(p.status)}</div></div>
        </div>
      </div>
      <h4 class="section">登记带看 / 到店</h4>
      <div class="form-grid">
        <div class="form-row"><label>客户<select id="schCust">${custOpts}</select></label></div>
        <div class="form-row"><label>带看经纪人 / 到店<select id="schAgent">${agentOpts}</select></label></div>
        <div class="form-row"><label>预约时间<input id="schTime" type="datetime-local" value="2026-10-08T10:00"></label></div>
        <div class="form-row" style="align-self:end"><button id="schBtn">登记带看</button></div>
      </div>
      <p class="hint">外店经纪人登记需房源方门店主管已签发跨店协作授权，否则将被系统拒绝并留痕。</p>
      <h4 class="section">带看时间线</h4>
      <ul class="timeline">${tl}</ul>
    </div>`;
}

function wirePropertyActions(p) {
  $("#schBtn").onclick = async () => {
    try {
      const v = await api("POST", "/api/viewings", {
        property_id: p.property_id,
        customer_id: $("#schCust").value,
        agent_id: $("#schAgent").value,
        appointment_time: $("#schTime").value.replace("T", " "),
        ide_key: `${p.property_id}:${$("#schCust").value}:${$("#schTime").value}`,
      });
      toast("带看已登记，确认短信已（模拟）发送", "ok");
      await loadCustomers();
      showProperty(p.property_id);
      if (v.customer_id) loadCalendar();
    } catch (e) { toast(e.message, "err"); }
  };
}

// ------------------------------------------------------------------ 客户
function bindCustomers() {
  $("#custSearchBtn").onclick = loadCustomers;
  $("#custQ").addEventListener("keydown", (e) => e.key === "Enter" && loadCustomers());
  $("#dupCheckBtn").onclick = async () => {
    try {
      const r = await api("POST", "/api/customers", {
        check_duplicates: true,
        name: $("#ncName").value.trim(), phone: $("#ncPhone").value.trim(),
      });
      const box = $("#dupResult");
      if (!r.duplicates.length) { box.innerHTML = '<span class="tag ok">未发现重复客户</span>'; return; }
      box.innerHTML = r.duplicates.map((d) =>
        `<div class="hit"><b>${d.dup_type === "exact" ? "精确重复" : "疑似重复"}</b>：${esc(d.name)} · ${esc(d.phone)}（${esc(d.customer_id)}）</div>`
      ).join("");
    } catch (e) { toast(e.message, "err"); }
  };
  $("#ncSubmitBtn").onclick = submitNewCustomer;
}

async function submitNewCustomer() {
  try {
    const c = await api("POST", "/api/customers", {
      name: $("#ncName").value.trim(),
      phone: $("#ncPhone").value.trim(),
      source_agent_id: state.actor,
      note: $("#ncNote").value.trim(),
    });
    toast(`客户已登记：${c.customer_id}`, "ok");
    $("#ncName").value = ""; $("#ncPhone").value = ""; $("#ncNote").value = "";
    $("#dupResult").innerHTML = "";
    await loadCustomers();
  } catch (e) {
    if (/疑似重复/.test(e.message)) {
      if (confirm(e.message + "\n\n确认为不同人并强制登记？")) {
        try {
          await api("POST", "/api/customers", {
            name: $("#ncName").value.trim(), phone: $("#ncPhone").value.trim(),
            source_agent_id: state.actor, note: $("#ncNote").value.trim(), force: true,
          });
          toast("已人工确认非重复，登记完成", "ok");
          await loadCustomers();
        } catch (e2) { toast(e2.message, "err"); }
      }
    } else toast(e.message, "err");
  }
}

async function loadCustomers() {
  state.customers = await api("GET", "/api/customers?q=" + encodeURIComponent($("#custQ").value));
  $("#custList").innerHTML = state.customers.map((c) => `
    <li data-id="${c.customer_id}" class="${state.selectedCust === c.customer_id ? "selected" : ""}">
      <div class="l1">${esc(c.name)}${c.merged_into ? ' <span class="tag gray">已合并</span>' : ""}</div>
      <div class="l2 mono">${esc(c.phone)}</div>
      ${c.merged_into ? `<div class="l2">并入 ${esc(c.merged_into)}</div>` : ""}
    </li>`).join("") || '<li class="empty" style="cursor:default">无匹配客户</li>';
  $$("#custList li[data-id]").forEach((li) =>
    li.onclick = () => showCustomer(li.dataset.id));
}

async function showCustomer(cid) {
  state.selectedCust = cid;
  loadCustomers();
  const c = await api("GET", "/api/customers/" + cid);
  const rows = c.viewings.map((v) => `
    <div class="vrow">
      <div class="vtop">
        <div><b>${esc(v.property.address)}</b> · ${esc(v.appointment_time)}</div>
        ${statusTag(v.status)}</div>
      <div class="muted">${esc(agentName(v.agent_id))} · ${esc(v.store_name)} · 规则 ${esc(v.rule_version)}</div>
      ${protectionBox(v)}
    </div>`).join("") || '<p class="empty">暂无带看记录</p>';
  $("#custDetail").innerHTML = `
    <div class="panel">
      <div class="detail-head">
        <h2>${esc(c.name)}</h2>
        <div class="addr-line mono">${esc(c.phone)} · ${esc(c.customer_id)}</div>
        <div class="muted">首次登记：${esc(c.first_registered_at)} · 录入人 ${esc(agentName(c.source_agent_id))}</div>
        ${c.note ? `<div class="muted">备注：${esc(c.note)}</div>` : ""}
      </div>
      <h4 class="section">带看记录与保护期</h4>
      ${rows}
    </div>`;
}

// ------------------------------------------------------------------ 日历
function bindCalendar() {
  $("#calPrev").onclick = () => { state.cal.setMonth(state.cal.getMonth() - 1); loadCalendar(); };
  $("#calNext").onclick = () => { state.cal.setMonth(state.cal.getMonth() + 1); loadCalendar(); };
  $("#dayClose").onclick = () => { $("#dayPanel").hidden = true; };
}

async function loadCalendar() {
  const y = state.cal.getFullYear(), m = state.cal.getMonth() + 1;
  $("#calTitle").textContent = `${y} 年 ${m} 月`;
  state.calData = await api("GET", `/api/calendar?ym=${y}-${String(m).padStart(2, "0")}`);
  const first = new Date(y, state.cal.getMonth(), 1);
  const startDow = first.getDay();
  const daysInMonth = new Date(y, state.cal.getMonth() + 1, 0).getDate();
  const prevDays = new Date(y, state.cal.getMonth(), 0).getDate();
  const today = "2026-10-01"; // 与种子/演示口径一致
  let html = ["日", "一", "二", "三", "四", "五", "六"]
    .map((w) => `<div class="cal-dow">周${w}</div>`).join("");
  const cell = (dayNum, other, dateStr) => {
    const list = (state.calData.days[dateStr] || []);
    const conflict = list.length > 1 &&
      new Set(list.map((v) => v.agent_id + v.property_id + v.customer_id)).size > 1 &&
      new Set(list.map((v) => v.agent_id)).size > 1;
    const items = list.map((v) =>
      `<div class="cal-item ${v.status} ${conflict ? "conflicted" : ""}" title="${esc(agentName(v.agent_id))} · ${esc(v.property.address)}">${esc(v.appointment_time.slice(11))} ${esc(agentName(v.agent_id).slice(0, 8))}·${esc(v.property.address.slice(0, 6))}</div>`
    ).join("");
    return `<div class="cal-cell ${other ? "other" : ""} ${dateStr === today ? "today" : ""}" data-date="${dateStr}">
      <div class="cal-date">${dayNum}</div>${items}</div>`;
  };
  for (let i = startDow - 1; i >= 0; i--)
    html += cell(prevDays - i, true, "");
  for (let dnum = 1; dnum <= daysInMonth; dnum++) {
    const ds = `${y}-${String(m).padStart(2, "0")}-${String(dnum).padStart(2, "0")}`;
    html += cell(dnum, false, ds);
  }
  // 补齐到完整周（7 的倍数）
  let trailing = 1;
  while (html.match(/class="cal-cell/g).length % 7 !== 0) {
    html += cell(trailing++, true, "");
  }
  $("#calendar").innerHTML = html;
  $$(".cal-cell[data-date]:not([data-date=''])").forEach((el) =>
    el.onclick = () => showDay(el.dataset.date));
}

async function showDay(dateStr) {
  $("#dayPanel").hidden = false;
  $("#dayTitle").textContent = `${dateStr} 带看安排`;
  const list = await api("GET", "/api/viewings?date=" + dateStr);
  const multi = new Set(list.map((v) => v.property_id + "|" + v.customer_id)).size < list.length
    && list.length > 1;
  $("#dayViewings").innerHTML = list.map((v) => {
    const sameSlotOthers = list.filter((o) =>
      o !== v && o.appointment_time === v.appointment_time && o.customer_id === v.customer_id);
    return `<div class="vrow" id="vrow-${v.viewing_id}">
      <div class="vtop">
        <div><b>${esc(v.property.address)}</b> · ${esc(v.appointment_time.slice(11))}<br>
          <span class="muted">客户 ${esc(v.customer.name)}（${esc(v.customer.phone)}）· ${esc(agentName(v.agent_id))} · ${esc(v.store_name)}</span></div>
        <div>${statusTag(v.status)}</div>
      </div>
      ${sameSlotOthers.length ? '<div class="protect-box">⚠ 同一客户在此时段存在多名经纪人带看，成交时将进入主管裁决候选。</div>' : ""}
      <div id="vactions-${v.viewing_id}" class="vactions"></div>
    </div>`;
  }).join("") || '<p class="empty">当天无安排</p>';
  list.forEach((v) => wireViewingRow(v));

  // 当天登记
  const propOpts = state.props.map((p) => `<option value="${p.property_id}">${esc(p.address)}</option>`).join("");
  const custOpts = state.customers.map((c) => `<option value="${c.customer_id}">${esc(c.name)}</option>`).join("");
  const agentOpts = state.meta.agents.map((a) => `<option value="${a.agent_id}">${esc(a.name)}（${esc(storeName(a.store_id))}）</option>`).join("")
    + `<option value="${state.meta.walkin}">客户自行到店</option>`;
  $("#daySchedule").innerHTML = `
    <h4 class="section">为 ${dateStr} 增补登记</h4>
    <div class="form-grid">
      <div class="form-row"><label>房源<select id="dProp">${propOpts}</select></label></div>
      <div class="form-row"><label>客户<select id="dCust">${custOpts}</select></label></div>
      <div class="form-row"><label>经纪人<select id="dAgent">${agentOpts}</select></label></div>
      <div class="form-row"><label>时刻<input id="dTime" type="time" value="14:00"></label></div>
    </div>
    <div class="form-actions"><button id="dAdd">登记</button></div>`;
  $("#dAdd").onclick = async () => {
    try {
      await api("POST", "/api/viewings", {
        property_id: $("#dProp").value, customer_id: $("#dCust").value,
        agent_id: $("#dAgent").value,
        appointment_time: `${dateStr} ${$("#dTime").value}`,
        ide_key: `${$("#dProp").value}:${$("#dCust").value}:${dateStr} ${$("#dTime").value}`,
      });
      toast("登记成功", "ok");
      await loadCalendar(); showDay(dateStr);
    } catch (e) { toast(e.message, "err"); }
  };
}

function wireViewingRow(v) {
  const box = $(`#vactions-${v.viewing_id}`);
  if (!box) return;
  let html = "";
  if (v.status === "scheduled") {
    html += `<button class="small" data-act="checkin">签到 + 客户签字</button>
             <button class="small danger-ghost" data-act="cancel">取消带看</button>`;
  }
  if (v.status === "checked" && !v.confirmed_at) {
    html += `<span class="confirm-line">客户确认码 <input id="cc-${v.viewing_id}" placeholder="6 位短信码">
             <button class="small ghost" data-act="confirm">客户确认</button></span>`;
  }
  if (v.confirmed_at) html += `<span class="tag ok">客户已确认 ${esc(v.confirmed_at)}</span>`;
  box.innerHTML = html;
  box.querySelectorAll("button").forEach((b) => b.onclick = async () => {
    const act = b.dataset.act;
    try {
      if (act === "cancel") {
        const reason = prompt("取消原因（将留痕，且不构成归属证据）：", "客户临时有事");
        if (reason === null) return;
        await api("POST", `/api/viewings/${v.viewing_id}/cancel`,
          { actor: state.actor, reason });
        toast("已取消并记录", "ok");
      } else if (act === "checkin") {
        openSignModal(v); return;
      } else if (act === "confirm") {
        await api("POST", `/api/viewings/${v.viewing_id}/confirm`,
          { code: $(`#cc-${v.viewing_id}`).value });
        toast("客户确认完成，该带看成为有效归属证据", "ok");
      }
      await loadCalendar();
      showDay(v.appointment_time.slice(0, 10));
      if (state.selectedProp) showProperty(state.selectedProp);
    } catch (e) { toast(e.message, "err"); }
  });
}

// ------------------------------------------------------------------ 签字
function bindSignModal() {
  const canvas = $("#signCanvas");
  const ctx = canvas.getContext("2d");
  let drawing = false;
  const pos = (e) => {
    const r = canvas.getBoundingClientRect();
    const pt = e.touches ? e.touches[0] : e;
    return { x: (pt.clientX - r.left) * canvas.width / r.width,
             y: (pt.clientY - r.top) * canvas.height / r.height };
  };
  const start = (e) => { drawing = true; const p = pos(e); ctx.beginPath(); ctx.moveTo(p.x, p.y); e.preventDefault(); };
  const move = (e) => { if (!drawing) return; const p = pos(e); ctx.lineTo(p.x, p.y); ctx.stroke(); e.preventDefault(); };
  const end = () => { drawing = false; };
  canvas.addEventListener("mousedown", start);
  canvas.addEventListener("mousemove", move);
  window.addEventListener("mouseup", end);
  canvas.addEventListener("touchstart", start, { passive: false });
  canvas.addEventListener("touchmove", move, { passive: false });
  canvas.addEventListener("touchend", end);
  ctx.lineWidth = 2.2; ctx.lineCap = "round"; ctx.strokeStyle = "#26262b";

  $("#signClear").onclick = () => ctx.clearRect(0, 0, canvas.width, canvas.height);
  $("#signSave").onclick = async () => {
    if (!state.signContext) return;
    const v = state.signContext;
    const blank = ctx.getImageData(0, 0, canvas.width, canvas.height).data
      .some((x, i) => i % 4 === 3 && x !== 0);
    if (!blank) return toast("请先让客户签名", "err");
    let lat = 0, lng = 0;
    const geo = $("#signGeo").value.trim();
    if (geo) { const m = geo.match(/(-?[\d.]+)\s*,\s*(-?[\d.]+)/); if (m) { lat = +m[1]; lng = +m[2]; } }
    try {
      await api("POST", `/api/viewings/${v.viewing_id}/checkin`, {
        actor: state.actor, lat, lng,
        signature_data_url: canvas.toDataURL("image/png"),
      });
      toast("签到完成，签字图已本地留存", "ok");
      $("#signModal").hidden = true;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      state.signContext = null;
      await loadCalendar();
      if ($("#dayPanel") && !$("#dayPanel").hidden) showDay(v.appointment_time.slice(0, 10));
      if (state.selectedProp) showProperty(state.selectedProp);
    } catch (e) { toast(e.message, "err"); }
  };
}

function openSignModal(v) {
  state.signContext = v;
  $("#signGeo").value = "39.9900,116.4700";
  $("#signModal").hidden = false;
}

// ------------------------------------------------------------------ 争议
function bindDisputes() {
  $("#dealFilter").onchange = loadDeals;
  $("#newDealBtn").onclick = showDealForm;
}

async function showDealForm() {
  const propOpts = state.props.map((p) => `<option value="${p.property_id}">${esc(p.address)}（${yuan(p.price_cents)}）</option>`).join("");
  const custOpts = state.customers.map((c) => `<option value="${c.customer_id}">${esc(c.name)} ${esc(c.phone)}</option>`).join("");
  $("#dealDetail").innerHTML = `
    <div class="panel">
      <h3>登记成交</h3>
      <div class="form-grid">
        <div class="form-row"><label>成交房源<select id="dlProp">${propOpts}</select></label></div>
        <div class="form-row"><label>成交客户<select id="dlCust">${custOpts}</select></label></div>
        <div class="form-row"><label>成交金额（元）<input id="dlAmount" type="number" value="3680000"></label></div>
        <div class="form-row"><label>成交日期<input id="dlDate" type="date" value="2026-10-20"></label></div>
      </div>
      <p class="hint">系统将依据当前签署规则版本，按已签到且客户确认的有效带看计算保护期与候选归属；保护期外成交不产生经纪人候选。</p>
      <div class="form-actions"><button class="ghost" id="dlCancel">取消</button><button id="dlSave">登记并生成候选</button></div>
    </div>`;
  $("#dlCancel").onclick = () => { $("#dealDetail").innerHTML = ""; };
  $("#dlSave").onclick = async () => {
    try {
      const deal = await api("POST", "/api/deals", {
        property_id: $("#dlProp").value,
        customer_id: $("#dlCust").value,
        amount_cents: Math.round(Number($("#dlAmount").value) * 100),
        deal_date: $("#dlDate").value,
        actor: state.actor,
      });
      toast(`成交已登记：${deal.status}`, "ok");
      await loadDeals();
      showDeal(deal.deal_id);
    } catch (e) { toast(e.message, "err"); }
  };
}

async function loadDeals() {
  state.deals = await api("GET", "/api/deals?status=" + encodeURIComponent($("#dealFilter").value));
  $("#dealList").innerHTML = state.deals.map((d) => `
    <li data-id="${d.deal_id}" class="${state.selectedDeal === d.deal_id ? "selected" : ""}">
      <div class="l1">${esc(d.property.address)} · ${esc(d.customer.name)}</div>
      <div class="l2">${d.deal_date} 成交 · ${yuan(d.amount_cents)} · <span class="mono">规则 ${esc(d.rule_version)}</span></div>
      <div class="tags">${statusTag(d.status)}
        ${d.candidates.map((c) => `<span class="tag ${c.rank_no === 1 ? "copper" : "gray"}">${esc(c.agent_id ? c.agent.name : c.store_name + "（公盘）")} ${fmtPct(c.share_pct)}</span>`).join("")}
      </div>
    </li>`).join("") || '<li class="empty" style="cursor:default">暂无成交</li>';
  $$("#dealList li[data-id]").forEach((li) =>
    li.onclick = () => showDeal(li.dataset.id));
}

function evidenceCard(c, deal) {
  const vids = c.viewing_ids || [];
  const views = deal.evidence.filter((e) => vids.includes(e.viewing_id));
  const evHtml = views.map((e) => `
    <div class="ev-row">
      ${esc(e.appointment_time)} · ${statusTag(e.status)}
      ${e.collab ? '<span class="tag copper">跨店</span>' : ""}
      ${e.checked_in_at ? `签到 ${esc(e.checked_in_at)}` : '<span class="tag danger">未签到</span>'}
      ${e.confirmed_at ? `<span class="tag ok">客户已确认</span>` : '<span class="tag warn">客户未确认</span>'}
      ${e.status === "cancelled" ? `<span class="tag danger">已取消：${esc(e.cancel_reason || "")}</span>` : ""}
      ${e.sign_path ? `<br><a href="${e.sign_path}" target="_blank"><img src="${e.sign_path}" alt="签字"></a>` : ""}
    </div>`).join("") || '<div class="ev-row muted">无关联有效带看</div>';
  return `
    <div class="cand-card ${c.rank_no === 1 ? "rank1" : ""}">
      <div class="cand-head">
        <div><div class="cand-name">#${c.rank_no} ${esc(c.agent_id ? c.agent.name : c.store_name + " · 门店公盘")}</div>
        <div class="muted">${esc(c.store_name)}${c.agent_id ? "" : "（客户自行到店份额）"}</div></div>
        <div class="cand-share">${fmtPct(c.share_pct)}</div>
      </div>
      <div class="cand-amount">对应佣金 ${yuan(c.amount_cents)}</div>
      <div class="cand-basis">${esc(c.basis)}</div>
      <div class="cand-ev"><div class="muted">证据（签到 / 客户签字 / 确认）</div>${evHtml}</div>
    </div>`;
}

async function showDeal(did) {
  state.selectedDeal = did;
  loadDeals();
  const d = await api("GET", "/api/deals/" + did);
  const sup = state.meta.agents.find((a) => a.agent_id === state.actor);
  const isSup = sup && sup.role === "supervisor";
  const isCandidate = d.candidates.some((c) => c.agent_id === state.actor);

  let rulingHtml = "";
  if (d.status === "保护期外") {
    rulingHtml = `<div class="panel"><h3>保护期判定</h3>
      <p>成交日 <b>${d.deal_date}</b> 不在任何「已签到 + 客户确认」带看的保护期窗口内
      （规则 ${esc(d.rule_version)}：保护期为签到次日起 ${d.rule.body.protection_days} 天）。
      该成交不产生经纪人佣金候选；如客户主张曾被带看，请补签到与客户确认后由主管复核。</p>
      <div class="muted">全部带看证据仍保留如下，供人工核查：</div>
      ${allEvidenceTable(d)}
    </div>`;
  } else if (d.status === "已裁决" && d.ruling) {
    const r = d.ruling;
    rulingHtml = `<div class="ruling-box">
      <h3>主管裁决（留痕不可改）</h3>
      <p>裁决人：<b>${esc(r.decided_by_name)}</b> · ${esc(r.decided_at)} · 依据规则版本 <span class="rv">${esc(r.rule_version)}</span></p>
      <p><b>裁决分成：</b>${r.split.map((s) => `${esc(s.agent_id ? agentName(s.agent_id) : storeName(s.store_id) + "（公盘）")} ${fmtPct(s.share_pct)}`).join("；")}</p>
      <p><b>理由：</b>${esc(r.rationale)}</p>
      <details><summary class="muted">裁决时冻结的证据快照（签到/签字/客户确认）</summary>
        <pre class="mono" style="white-space:pre-wrap;max-height:260px;overflow:auto">${esc(JSON.stringify(r.evidence, null, 2))}</pre>
      </details>
    </div>
      <h4 class="section">全部带看证据</h4>${allEvidenceTable(d)}`;
  } else if (d.status === "待裁决") {
    const editorRows = d.candidates.map((c, i) => `
      <tr data-key="${esc(c.agent_id || "store:" + c.store_id)}">
        <td>${esc(c.agent_id ? c.agent.name : c.store_name + "（公盘）")}</td>
        <td><input type="number" step="0.01" value="${c.share_pct}" class="split-pct"></td>
        <td class="muted">${yuan(c.amount_cents)}</td>
      </tr>`).join("");
    const canRule = isSup && !isCandidate;
    rulingHtml = `
      <div class="panel split-editor">
        <h3>主管裁决</h3>
        <p class="hint">裁决必须基于：签署规则版本（${esc(d.rule_version)}，全文见下）、签到记录与客户确认。经纪人不能裁决自己的争议。</p>
        ${!isSup ? '<div class="protect-box">当前操作人不是主管，仅可查看证据。</div>' : ""}
        ${isSup && isCandidate ? '<div class="protect-box" style="border-color:#d8a8a3;background:var(--danger-bg);color:var(--danger)">利益回避：您是本案候选经纪人，不能裁决自己的归属争议。请切换为非候选主管。</div>' : ""}
        <table><thead><tr><th>候选</th><th>裁决分成 %</th><th>系统建议金额</th></tr></thead><tbody>${editorRows}</tbody></table>
        <div class="form-row"><label>裁决理由（留痕）<textarea id="ruleReason" rows="3" placeholder="引用首带/末带、客户确认、跨店授权等事实"></textarea></label></div>
        <div class="form-actions">
          <span class="muted">分成合计须为 100%</span>
          <button id="ruleSubmit" ${canRule ? "" : "disabled"}>提交裁决并冻结证据</button>
        </div>
        <details style="margin-top:10px"><summary>裁决依据：规则 ${esc(d.rule_version)} 全文</summary>
          <pre class="mono" style="white-space:pre-wrap;background:var(--paper-2);padding:10px;border:1px solid var(--line)">${esc(d.rule.text)}</pre></details>
      </div>`;
  } else {
    rulingHtml = `<div class="panel"><h3>系统建议归属</h3>
      <p>依据规则 ${esc(d.rule_version)} 仅产生一名归属方，无争议。若有异议可由主管在掌握新证据后介入（演示中可直接查看证据）。</p>
      ${allEvidenceTable(d)}</div>`;
  }

  $("#dealDetail").innerHTML = `
    <div class="deal-summary">
      <div><div class="k muted">房源</div><div class="v">${esc(d.property.address)}</div></div>
      <div><div class="k muted">客户</div><div class="v">${esc(d.customer.name)} · ${esc(d.customer.phone)}</div></div>
      <div><div class="k muted">成交日期</div><div class="v">${d.deal_date}</div></div>
      <div><div class="k muted">成交金额</div><div class="v amount">${yuan(d.amount_cents)}</div></div>
      <div><div class="k muted">规则版本</div><div class="v mono">${esc(d.rule_version)}</div></div>
      <div><div class="k muted">状态</div><div class="v">${statusTag(d.status)}</div></div>
    </div>
    <h4 class="section">候选经纪人证据（并列展示）</h4>
    <div class="candidates-grid">${
      d.candidates.length
        ? d.candidates.map((c) => evidenceCard(c, d)).join("")
        : '<div class="panel empty">无候选：无满足保护期与证据要求的带看。</div>'
    }</div>
    ${rulingHtml}`;

  const btn = $("#ruleSubmit");
  if (btn) btn.onclick = async () => {
    const split = $$(".split-editor tbody tr").map((tr) => {
      const key = tr.dataset.key;
      const pct = Number(tr.querySelector(".split-pct").value);
      return key.startsWith("store:")
        ? { store_id: key.slice(6), share_pct: pct }
        : { agent_id: key, share_pct: pct };
    });
    try {
      await api("POST", `/api/deals/${d.deal_id}/rule`, {
        decided_by: state.actor, split,
        rationale: $("#ruleReason").value.trim(),
      });
      toast("裁决已提交并留痕", "ok");
      await loadDeals();
      showDeal(d.deal_id);
      loadAudit();
    } catch (e) { toast(e.message, "err"); }
  };
}

function allEvidenceTable(d) {
  return `<table style="width:100%;border-collapse:collapse;margin-top:8px;font-size:13px">
    <thead><tr style="text-align:left;color:var(--ink-3)"><th style="padding:6px">时间</th><th>经纪人/门店</th><th>状态</th><th>签到</th><th>客户确认</th><th>签字</th><th>规则</th></tr></thead>
    <tbody>${d.evidence.map((e) => `
      <tr style="border-top:1px solid var(--line)">
        <td style="padding:6px" class="mono">${esc(e.appointment_time)}</td>
        <td>${esc(e.agent_name)}<br><span class="muted">${esc(storeName(e.store_id))}${e.collab ? " · 跨店协作" : ""}</span></td>
        <td>${statusTag(e.status)}${e.status === "cancelled" ? `<div class="muted">${esc(e.cancel_reason || "")}</div>` : ""}</td>
        <td>${e.checked_in_at ? esc(e.checked_in_at) : "—"}</td>
        <td>${e.confirmed_at ? esc(e.confirmed_at) : '<span class="tag warn">未确认</span>'}</td>
        <td>${e.sign_path ? `<a href="${e.sign_path}" target="_blank">查看</a>` : "—"}</td>
        <td class="mono">${esc(e.rule_version)}</td>
      </tr>`).join("")}</tbody></table>`;
}

// ------------------------------------------------------------------ 审计
async function loadAudit() {
  if (state.tab !== "audit") return;
  const a = await api("GET", "/api/audit");
  $("#eventFeed").innerHTML = a.events.map((e) => `
    <div class="ev"><span class="t">${esc(e.ts)}</span> <b>${esc(e.type)}</b>
      <span class="muted">by ${esc(e.actor || "—")}${e.rule_version ? " · 规则 " + esc(e.rule_version) : ""}</span>
      <div class="muted">${esc(e.stream_type)}/${esc(e.stream_id)}</div></div>`).join("");
  const sms = await api("GET", "/api/sms");
  $("#smsFeed").innerHTML = sms.map((s) => `
    <div class="sms"><span class="t">${esc(s.sent_at)}</span> → <b>${esc(s.phone)}</b>
    <div>${esc(s.content)}</div></div>`).join("") || '<p class="empty">暂无</p>';
  $("#deniedFeed").innerHTML = a.denied.map((x) => `
    <div class="ev bad"><span class="t">${esc(x.ts)}</span> <b>${esc(x.action)}</b>
    <div>actor=${esc(x.actor || "—")}</div><div>${esc(x.reason)}</div>
    <div class="muted">${esc(x.detail)}</div></div>`).join("") || '<p class="empty">无被拒绝动作</p>';
}

init().catch((e) => toast("初始化失败：" + e.message, "err"));
