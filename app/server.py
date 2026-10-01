"""零依赖 Web 服务器（http.server）：房源/客户检索、带看日历、签到、
保护期提示、佣金争议裁决页。"""
import html
import json
import os
import urllib.parse
from datetime import date, datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db as store
from . import domain as D
from .sms import send_sms

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "app.db")
SIG_ROOT = os.path.join(BASE_DIR, "data")
STATIC_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

STATUS_LABEL = {"scheduled": ("待签到", "amber"), "checked_in": ("已签到", "green"),
                "cancelled": ("已取消", "gray")}


def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def money(x):
    return f"¥{x:,.0f}" if x is not None else "—"


def pct(x):
    return f"{x * 100:.0f}%"


class Ctx:
    def __init__(self, conn, actor_id, flash=None, err=None):
        self.conn = conn
        self.actor_id = actor_id
        self.actor = D.get_agent(conn, actor_id) if actor_id else None
        self.flash = flash
        self.err = err


# ---------------- 页面外壳 ----------------

def layout(ctx, title, body, active=""):
    agents = ctx.conn.execute(
        "SELECT id, name, role FROM agents WHERE active=1 ORDER BY id").fetchall()
    opts = ['<option value="">— 未选择身份 —</option>']
    for a in agents:
        sel = " selected" if a["id"] == ctx.actor_id else ""
        role = "主管" if a["role"] == "supervisor" else "经纪人"
        opts.append(f'<option value="{a["id"]}"{sel}>{esc(a["name"])}（{role}）</option>')
    nav = [("/", "工作台"), ("/properties", "房源"), ("/clients", "客户"),
           ("/calendar", "带看日历"), ("/disputes", "佣金争议"), ("/rules", "规则版本"),
           ("/sms", "短信记录")]
    links = "".join(
        f'<a href="{u}"{" style=color:var(--copper)" if u == active else ""}>{t}</a>'
        for u, t in nav)
    flash = ""
    if ctx.flash:
        flash += f'<div class="flash ok">{esc(ctx.flash)}</div>'
    if ctx.err:
        flash += f'<div class="flash err">{esc(ctx.err)}</div>'
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 铜印房产业务系统</title>
<link rel="stylesheet" href="/static/style.css"></head>
<body>
<header class="topbar">
  <span class="brand"><span class="mark">◆</span> 铜印房产</span>
  <nav class="main">{links}</nav>
  <form class="actor-box" method="post" action="/switch-actor">
    当前操作身份
    <select name="actor_id" onchange="this.form.submit()">{''.join(opts)}</select>
  </form>
</header>
<main class="wrap">
{flash}
{body}
</main>
<div class="footer-note">事件溯源 SQLite · 签名证据本地保存 · 短信为模拟适配器 · 裁决锁定规则版本</div>
<script src="/static/pad.js"></script>
</body></html>"""


# ---------------- 工作台 ----------------

def page_home(ctx):
    conn = ctx.conn
    today = date.today().isoformat()
    open_d = conn.execute("SELECT COUNT(*) c FROM disputes WHERE status='open'").fetchone()["c"]
    today_n = conn.execute(
        "SELECT COUNT(*) c FROM viewings WHERE date(appointment_ts)=? AND status!='cancelled'",
        (today,)).fetchone()["c"]
    prot = conn.execute(
        """SELECT COUNT(DISTINCT v.client_id||'-'||v.property_id) c FROM viewings v
           WHERE v.status='checked_in'""").fetchone()["c"]
    disputes = conn.execute(
        """SELECT d.*, p.address, c.canonical_name, de.commission, de.amount, rv.version
           FROM disputes d JOIN deals de ON de.id=d.deal_id
           JOIN properties p ON p.id=de.property_id
           JOIN clients c ON c.id=de.client_id
           JOIN rule_versions rv ON rv.id=d.rule_version_id
           ORDER BY d.status DESC, d.id""").fetchall()
    rows = "".join(
        f"""<tr>
          <td><a href="/dispute/{d['id']}">#{d['id']}</a></td>
          <td>{esc(d['address'])}</td>
          <td>{esc(d['canonical_name'])}</td>
          <td class="money">{money(d['amount'])}</td>
          <td class="money">{money(d['commission'])}</td>
          <td><span class="rule-ver">{esc(d['version'])}</span></td>
          <td>{('<span class="tag red">待主管裁决</span>' if d['status']=='open'
                else '<span class="tag green">已裁决</span>')}</td>
        </tr>""" for d in disputes)
    body = f"""
<h1>工作台</h1>
<div class="sub">登记房源与客户、带看签到、保护期提示、佣金归属候选与主管裁决。{today}</div>
<div class="card">
  <div class="facts">
    <div class="f"><div class="n">{today_n}</div><div class="l">今日有效带看</div></div>
    <div class="f"><div class="n">{open_d}</div><div class="l">待裁决争议</div></div>
    <div class="f"><div class="n">{prot}</div><div class="l">有签到证据的客户-房源</div></div>
  </div>
</div>
<h2>检索</h2>
<div class="grid2">
  <form class="card" action="/properties" method="get">
    <label class="fld">房源（地址 / 区域）
      <div class="searchbar" style="margin-top:4px">
        <input type="text" name="q" placeholder="如：长江路 / 中山区">
        <button type="submit">检索房源</button>
      </div></label>
    <div class="small muted">或 <a href="/properties">查看全部房源</a></div>
  </form>
  <form class="card" action="/clients" method="get">
    <label class="fld">客户（姓名 / 手机号 / 证件号）
      <div class="searchbar" style="margin-top:4px">
        <input type="text" name="q" placeholder="手机号或证件号自动去重">
        <button type="submit">检索客户</button>
      </div></label>
    <div class="small muted">或 <a href="/clients">查看全部客户</a></div>
  </form>
</div>
<h2>佣金争议</h2>
<div class="card"><table>
<thead><tr><th>编号</th><th>房源</th><th>客户</th><th>成交价</th><th>佣金金额</th>
<th>规则版本</th><th>状态</th></tr></thead><tbody>{rows or '<tr><td colspan=7 class=muted>暂无</td></tr>'}</tbody>
</table></div>"""
    return layout(ctx, "工作台", body, "/")


# ---------------- 房源 ----------------

def page_properties(ctx, q):
    rows = D.search_properties(ctx.conn, q or "")
    items = "".join(
        f"""<tr>
        <td><a class="address" href="/property/{p['id']}">{esc(p['address'])}</a></td>
        <td>{esc(p['district'])}</td>
        <td class="money">{money(p['price'])}</td>
        <td>{esc(p['store_name'])}</td>
        <td>{({'on':'<span class="tag green">在售</span>','dealt':'<span class="tag copper">已成交</span>',
               'off':'<span class="tag gray">下架</span>'}[p['status']])}</td>
        </tr>""" for p in rows)
    dup = D.find_duplicates(ctx.conn, q) if q else []
    body = f"""
<h1>房源检索</h1>
<form class="searchbar" method="get" style="margin:12px 0">
  <input type="text" name="q" value="{esc(q)}" placeholder="地址或区域关键词">
  <button>检索</button>
</form>
<div class="card"><table>
<thead><tr><th>地址</th><th>区域</th><th>挂牌价</th><th>房源方</th><th>状态</th></tr></thead>
<tbody>{items or '<tr><td colspan=5 class=muted>无匹配房源</td></tr>'}</tbody></table></div>"""
    return layout(ctx, "房源检索", body, "/properties")


def _timeline(ctx, property_id):
    rows = D.property_timeline(ctx.conn, property_id)
    today = date.today().isoformat()
    items = []
    for v in rows:
        st_cls = v["status"]
        label, tagcls = STATUS_LABEL[v["status"]]
        who = "客户自行到店" if v["is_self_visit"] else f"{esc(v['agent_name'])}（{esc(v['store_name'])}）"
        cross = '<span class="tag copper">跨店协作</span>' if v["cross_store"] else ""
        ci = f"<br><span class='small muted'>签到 {esc(v['checkin_ts'])}</span>" if v["checkin_ts"] else ""
        confirm = (' <span class="tag green">客户已确认</span>' if v["client_confirmed"]
                   else (' <span class="tag amber">待客户确认</span>' if v["status"] == "checked_in" else ""))
        sig = ""
        if v["status"] == "checked_in":
            sig = f' · <a href="/checkin/{v["id"]}">查看签到与签字</a>'
        acts = ""
        if v["status"] == "scheduled":
            acts = (f' · <a href="/checkin/{v["id"]}">前往签到</a>'
                    f' · <form class="inline" method="post" action="/viewings/{v["id"]}/cancel">'
                    f'<a href="#" onclick="this.closest(\'form\').submit();return false;">取消带看</a></form>')
        elif v["status"] == "checked_in" and not v["client_confirmed"] and ctx.actor and ctx.actor["role"] == "supervisor":
            acts = (f' · <form class="inline" method="post" action="/viewings/{v["id"]}/confirm">'
                    f'<a href="#" onclick="this.closest(\'form\').submit();return false;">主管补录客户确认</a></form>')
        items.append(
            f"""<li class="{st_cls}">
              <div><b>{esc(v['appointment_ts'].replace('T',' ').replace('Z',''))}</b>
                <span class="tag {tagcls}">{label}</span> {cross}{confirm}</div>
              <div class="small">客户：{esc(v['client_name'])} · 带看人：{who}{ci}{sig}{acts}</div>
              {f'<div class="small muted">备注：{esc(v["note"])}</div>' if v['note'] else ''}
            </li>""")
    return f'<ol class="timeline">{"".join(items) or "<li class=muted>暂无带看</li>"}</ol>'


def page_property(ctx, pid):
    conn = ctx.conn
    p = D.get_property(conn, pid)
    if not p:
        return layout(ctx, "未找到", "<p>房源不存在。</p>")
    store_row = D.get_store(conn, p["store_id"])
    clients = conn.execute("SELECT id, canonical_name FROM clients ORDER BY id").fetchall()
    agents = conn.execute(
        "SELECT id, name, store_id, can_cross_store FROM agents WHERE role='agent' AND active=1 ORDER BY id"
    ).fetchall()
    client_opts = "".join(
        f'<option value="{c["id"]}">{c["id"]}. {esc(c["canonical_name"])}</option>' for c in clients)
    agent_opts = "".join(
        f'<option value="{a["id"]}"{" selected" if a["id"]==ctx.actor_id else ""}>'
        f'{esc(a["name"])}（门店{a["store_id"]}{"，可跨店" if a["can_cross_store"] else ""}）</option>'
        for a in agents)

    # 保护期提示：按已带看客户逐个展示当前状态
    prot_rows = []
    cids = [r["client_id"] for r in conn.execute(
        "SELECT DISTINCT client_id FROM viewings WHERE property_id=?", (pid,)).fetchall()]
    today = date.today().isoformat()
    for cid in cids:
        st = D.protection_status(conn, cid, pid, today)
        c = D.get_client(conn, cid)
        if st["protected"]:
            prot_rows.append(
                f'<div class="protection-box"><b>{esc(c["canonical_name"])}</b> 处于保护期内'
                f'（规则 <span class="rule-ver">{esc(st["rule_version"])}</span>，'
                f'首看 {st["first_date"]}，保护至 {st["until"]}，共 {st["window_days"]} 天）</div>')
        else:
            if st["basis_viewings"]:
                prot_rows.append(
                    f'<div class="protection-box off">{esc(c["canonical_name"])} 保护期已结束'
                    f'（首看 {st["first_date"]}，{st["until"]} 到期；此日后成交归房源方门店）</div>')
    prot_html = "".join(prot_rows) or '<div class="small muted">尚无带看，暂无保护期。</div>'

    deals = conn.execute(
        """SELECT de.*, c.canonical_name, rv.version FROM deals de
           JOIN clients c ON c.id=de.client_id JOIN rule_versions rv ON rv.id=de.rule_version_id
           WHERE de.property_id=? ORDER BY de.id""", (pid,)).fetchall()
    deal_html = "".join(
        f"""<tr><td>{esc(d['deal_date'])}</td><td>{esc(d['canonical_name'])}</td>
        <td class="money">{money(d['amount'])}</td><td class="money">{money(d['commission'])}</td>
        <td><span class="rule-ver">{esc(d['version'])}</span></td>
        <td>{('<span class="tag green">保护期内</span>' if d['protection_active']
              else '<span class="tag gray">保护期外</span>')}</td></tr>""" for d in deals)

    body = f"""
<h1>{esc(p['address'])}</h1>
<div class="sub">{esc(p['district'])} · 房源方：{esc(store_row['name'])} ·
挂牌价 <span class="money">{money(p['price'])}</span></div>
<div class="grid2">
  <div>
    <h2>保护期提示</h2>
    <div class="card">{prot_html}</div>
    <h2>带看时间线</h2>
    <div class="card">{_timeline(ctx, pid)}</div>
  </div>
  <div>
    <h2>登记带看</h2>
    <div class="card">
      <form method="post" action="/viewings">
        <input type="hidden" name="property_id" value="{pid}">
        <label class="fld">预约时间<input type="datetime-local" name="appointment_ts" required></label>
        <label class="fld">客户
          <select name="client_id"><option value="">— 选择已有客户 —</option>{client_opts}</select></label>
        <div class="small muted" style="margin:-4px 0 8px">新客户：</div>
        <div class="grid2">
          <label class="fld">新客户姓名<input type="text" name="new_client_name"></label>
          <label class="fld">手机号<input type="text" name="new_client_phone"></label>
        </div>
        <label class="fld">证件号（选填，强去重）<input type="text" name="new_client_idno"></label>
        <label class="fld">带看经纪人
          <select name="agent_id">{agent_opts}</select></label>
        <label class="fld"><input type="checkbox" name="self_visit" value="1" style="width:auto">
          客户自行到店（无经纪人，佣金归房源方门店）</label>
        <label class="fld"><input type="checkbox" name="cross_confirm" value="1" style="width:auto">
          确认跨门店协作登记（仅可跨店经纪人能提交他店房源）</label>
        <label class="fld">备注<input type="text" name="note"></label>
        <button type="submit">登记带看</button>
        <div class="small muted" style="margin-top:8px">
          同一经纪人同一天重复登记同一客户/房源将被数据库拦截，不会形成双记录。</div>
      </form>
    </div>
    <h2>成交记录</h2>
    <div class="card"><table>
      <thead><tr><th>日期</th><th>客户</th><th>成交价</th><th>佣金</th><th>规则版本</th><th>保护期</th></tr></thead>
      <tbody>{deal_html or '<tr><td colspan=6 class=muted>暂无成交</td></tr>'}</tbody></table></div>
    <div class="card">
      <form method="post" action="/deals" class="small">
        <input type="hidden" name="property_id" value="{pid}">
        <b>登记成交</b>
        <div class="grid3" style="margin-top:6px">
          <label class="fld">客户<select name="client_id">{client_opts}</select></label>
          <label class="fld">成交日<input type="date" name="deal_date" value="{today}"></label>
          <label class="fld">成交价<input type="number" name="amount" value="{int(p['price'])}"></label>
        </div>
        <div class="grid3">
          <label class="fld">佣金金额<input type="number" name="commission" value="{int(p['price']*0.01)}"></label>
          <label class="fld" style="align-self:end"><button>登记成交并计算候选</button></label>
        </div>
      </form>
    </div>
  </div>
</div>"""
    return layout(ctx, p["address"], body, "/properties")


# ---------------- 客户 ----------------

def page_clients(ctx, q):
    rows = D.search_clients(ctx.conn, q or "")
    dup_note = ""
    if q:
        hits = D.find_duplicates(ctx.conn, q)
        if len(hits) > 1:
            dup_note = (f'<div class="flash info">“{esc(q)}” 匹配到 {len(hits)} 个客户档案，'
                        '请核对手机号/证件号；强标识相同的留资在登记时会自动合并，不另建档案。</div>')
    items = "".join(
        f"""<tr><td>{c['id']}</td><td><a href="/client/{c['id']}"><b>{esc(c['canonical_name'])}</b></a></td>
        <td class="small">{esc(c['identities'])}</td></tr>""" for c in rows)
    body = f"""
<h1>客户检索与去重</h1>
{dup_note}
<form class="searchbar" method="get" style="margin:12px 0">
  <input type="text" name="q" value="{esc(q)}" placeholder="姓名 / 手机号 / 证件号">
  <button>检索</button>
</form>
<div class="card"><table>
<thead><tr><th>ID</th><th>姓名</th><th>身份标识（手机/证件/别名）</th></tr></thead>
<tbody>{items or '<tr><td colspan=3 class=muted>无匹配客户</td></tr>'}</tbody></table></div>"""
    return layout(ctx, "客户", body, "/clients")


def page_client(ctx, cid):
    conn = ctx.conn
    c = D.get_client(conn, cid)
    if not c:
        return layout(ctx, "未找到", "<p>客户不存在。</p>")
    aliases = conn.execute("SELECT * FROM client_aliases WHERE client_id=? ORDER BY id", (cid,)).fetchall()
    alias_html = "".join(
        f'<tr><td>{({"phone":"手机号","idno":"证件号","name":"姓名/别名"}[a["kind"]])}</td>'
        f'<td>{esc(a["value"])}</td></tr>' for a in aliases)
    views = conn.execute(
        """SELECT v.*, p.address FROM viewings v JOIN properties p ON p.id=v.property_id
           WHERE v.client_id=? ORDER BY v.appointment_ts""", (cid,)).fetchall()
    vh = "".join(
        f"""<tr><td>{esc(v['appointment_ts'][:16].replace('T',' '))}</td>
        <td><a href="/property/{v['property_id']}">{esc(v['address'])}</a></td>
        <td><span class="tag {STATUS_LABEL[v['status']][1]}">{STATUS_LABEL[v['status']][0]}</span></td>
        <td>{'<span class="tag copper">跨店</span>' if v['cross_store'] else ''}
            {'<span class="tag">自行到店</span>' if v['is_self_visit'] else ''}</td></tr>"""
        for v in views)
    body = f"""
<h1>客户 {esc(c['canonical_name'])}</h1>
<div class="grid2">
 <div class="card"><h2 style="margin-top:0">身份标识（去重依据）</h2>
   <table><tbody>{alias_html}</tbody></table>
   <div class="small muted" style="margin-top:8px">手机号或证件号相同的留资自动并入本档案，不产生重复客户。</div>
 </div>
 <div class="card"><h2 style="margin-top:0">带看记录</h2>
   <table><thead><tr><th>时间</th><th>房源</th><th>状态</th><th>类型</th></tr></thead>
   <tbody>{vh or '<tr><td colspan=4 class=muted>暂无</td></tr>'}</tbody></table>
 </div>
</div>"""
    return layout(ctx, c["canonical_name"], body, "/clients")


# ---------------- 日历 ----------------

def page_calendar(ctx, month, store_id):
    if not month:
        month = date.today().strftime("%Y-%m")
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    next_m = date(y + (m // 12), m % 12 + 1, 1)
    from datetime import timedelta
    start = first - timedelta(days=first.weekday())
    days = []
    cur = start
    while len(days) < 42:
        days.append(cur)
        cur += timedelta(days=1)
    evs = D.calendar_viewings(ctx.conn, days[0].isoformat(), days[-1].isoformat(),
                              store_id or None)
    by_day = {}
    for v in evs:
        by_day.setdefault(v["appointment_ts"][:10], []).append(v)
    stores = ctx.conn.execute("SELECT * FROM stores ORDER BY id").fetchall()
    store_opts = '<option value="">全部门店</option>' + "".join(
        f'<option value="{s["id"]}"{" selected" if str(s["id"])==str(store_id) else ""}>'
        f'{esc(s["name"])}</option>' for s in stores)
    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m_d = next_m.strftime("%Y-%m")
    today = date.today().isoformat()
    dows = "".join(f'<div class="dow">{d}</div>' for d in
                   ["周一", "周二", "周三", "周四", "周五", "周六", "周日"])
    cells = []
    for d in days:
        cls = "day" + (" out" if d.month != m else "") + (" today" if d.isoformat() == today else "")
        evhtml = ""
        for v in by_day.get(d.isoformat(), []):
            who = "自行到店" if v["is_self_visit"] else esc(v["agent_name"])
            st = v["status"]
            evhtml += (f'<a href="/checkin/{v["id"]}" class="evt {st}">'
                       f'{esc(v["appointment_ts"][11:16])} {who} · {esc(v["address"][:12])}</a>')
        cells.append(f'<div class="{cls}"><div class="dnum">{d.day}</div>{evhtml}</div>')
    body = f"""
<h1>带看日历</h1>
<div class="sub">
  <a href="/calendar?month={prev_m}&store={esc(store_id)}">← 上月</a> &nbsp;
  <b>{month}</b> &nbsp;
  <a href="/calendar?month={next_m_d}&store={esc(store_id)}">下月 →</a>
  <form method="get" style="display:inline-block;margin-left:18px">
    <input type="month" name="month" value="{month}" style="width:auto">
    <select name="store" style="width:auto">{store_opts}</select>
    <button class="ghost" type="submit">查看</button>
  </form>
</div>
<div class="cal">{dows}{''.join(cells)}</div>
<div class="small muted" style="margin-top:8px">点击任一带看进入签到/证据页；删除线为已取消，绿色为已签到。</div>"""
    return layout(ctx, "日历", body, "/calendar")


# ---------------- 签到 ----------------

def page_checkin(ctx, vid, err=None):
    conn = ctx.conn
    v = conn.execute(
        """SELECT v.*, p.address, c.canonical_name, a.name AS agent_name, s.name AS store_name
           FROM viewings v JOIN properties p ON p.id=v.property_id
           JOIN clients c ON c.id=v.client_id LEFT JOIN agents a ON a.id=v.agent_id
           JOIN stores s ON s.id=v.store_id WHERE v.id=?""", (vid,)).fetchone()
    if not v:
        return layout(ctx, "未找到", "<p>带看不存在。</p>")
    st = D.protection_status(conn, v["client_id"], v["property_id"], v["appointment_ts"][:10])
    who = "客户自行到店（无经纪人）" if v["is_self_visit"] else f"带看经纪人：{esc(v['agent_name'])}"
    if v["status"] == "checked_in":
        sig_a = (f'<figure><figcaption>经纪人签字</figcaption><img src="/{esc(v["sig_agent_path"])}"></figure>'
                 if v["sig_agent_path"] else "")
        body = f"""
<h1>带看 #{vid} 签到证据</h1>
<div class="sub">{esc(v['address'])} · 客户 {esc(v['canonical_name'])} · {who}</div>
<div class="card">
  <dl class="kv">
    <dt>预约时间</dt><dd>{esc(v['appointment_ts'].replace('T',' '))}</dd>
    <dt>签到时间</dt><dd>{esc(v['checkin_ts'])}</dd>
    <dt>客户确认</dt><dd>{('<span class="tag green">已确认</span> '+esc(v['client_confirmed_ts'])) if v['client_confirmed'] else '<span class="tag amber">尚未确认，不能作为裁决证据</span>'}</dd>
    <dt>规则版本</dt><dd><span class="rule-ver">{esc(st['rule_version'])}</span>（带看日生效，保护期 {st['window_days']} 天）</dd>
  </dl>
  <div class="sig-row" style="margin-top:10px">{sig_a}
    <figure><figcaption>客户签字</figcaption><img src="/{esc(v['sig_client_path'])}"></figure>
  </div>
</div>
{('<div class="card"><form method="post" action="/viewings/'+str(vid)+'/confirm">'
  '<button class="ghost" type="submit">主管补录客户电话/现场确认</button></form></div>')
 if not v['client_confirmed'] and ctx.actor and ctx.actor['role']=='supervisor' else ''}
"""
        return layout(ctx, f"签到 #{vid}", body, "/calendar")

    if v["status"] == "cancelled":
        return layout(ctx, f"带看 #{vid}",
                      '<div class="flash err">该带看已取消，不能签到。</div>', "/calendar")

    body = f"""
<h1>带看签到 #{vid}</h1>
<div class="sub">{esc(v['address'])} · 客户 {esc(v['canonical_name'])} · {esc(v['appointment_ts'].replace('T',' '))}</div>
{('<div class="flash err">'+esc(err)+'</div>') if err else ''}
<form method="post" action="/checkin/{vid}">
  <div class="grid2">
    {"" if v['is_self_visit'] else '''<div class="card"><h2 style="margin-top:0">经纪人签字</h2>
      <div class="pad-wrap"><canvas id="pad-agent" class="pad" data-hidden="sig-agent"></canvas>
      <div class="pad-actions"><button type="button" class="ghost" id="pad-agent-clear">重签</button></div></div>
      <input type="hidden" name="sig_agent" id="sig-agent"></div>'''}
    <div class="card"><h2 style="margin-top:0">客户签字</h2>
      <div class="pad-wrap"><canvas id="pad-client" class="pad" data-hidden="sig-client"></canvas>
      <div class="pad-actions"><button type="button" class="ghost" id="pad-client-clear">重签</button></div></div>
      <input type="hidden" name="sig_client" id="sig-client"></div>
  </div>
  <div class="card">
    <label><input type="checkbox" name="client_confirmed" value="1" style="width:auto">
      客户现场确认本次带看真实发生（截图与签字随证据留存）</label>
    <div style="margin-top:10px"><button type="submit">提交签到</button>
    <span class="small muted" style="margin-left:10px">签字图以 PNG 保存在本地 data/signatures，不随请求外泄。</span></div>
  </div>
</form>"""
    return layout(ctx, "签到", body, "/calendar")


# ---------------- 争议 ----------------

def page_disputes(ctx):
    rows = ctx.conn.execute(
        """SELECT d.id, d.status, d.topic, p.address, c.canonical_name,
                  de.amount, de.commission, de.deal_date, rv.version, rv.title
           FROM disputes d JOIN deals de ON de.id=d.deal_id
           JOIN properties p ON p.id=de.property_id
           JOIN clients c ON c.id=de.client_id
           JOIN rule_versions rv ON rv.id=d.rule_version_id
           ORDER BY d.status DESC, d.id""").fetchall()
    items = "".join(
        f"""<tr><td><a href="/dispute/{d['id']}">#{d['id']}</a></td>
        <td>{esc(d['address'])}</td><td>{esc(d['canonical_name'])}</td>
        <td>{esc(d['deal_date'])}</td>
        <td class="money">{money(d['amount'])} / 佣金 {money(d['commission'])}</td>
        <td><span class="rule-ver">{esc(d['version'])}</span></td>
        <td>{('<span class="tag red">待裁决</span>' if d['status']=='open' else '<span class="tag green">已裁决</span>')}</td>
        </tr>""" for d in rows)
    body = f"""
<h1>佣金争议</h1>
<div class="sub">候选经纪人的签到、签字与客户确认并列展示；主管裁决时锁定成交日生效的规则版本。</div>
<div class="card"><table>
<thead><tr><th>编号</th><th>房源</th><th>客户</th><th>成交日</th><th>成交价/佣金</th>
<th>规则版本</th><th>状态</th></tr></thead>
<tbody>{items or '<tr><td colspan=7 class=muted>暂无争议（独家首看或保护期外成交无需裁决）</td></tr>'}</tbody>
</table></div>"""
    return layout(ctx, "佣金争议", body, "/disputes")


def page_dispute(ctx, did, err=None):
    conn = ctx.conn
    d = D.get_dispute(conn, did)
    if not d:
        return layout(ctx, "未找到", "<p>争议不存在。</p>")
    deal = d["deal"]
    prop = D.get_property(conn, deal["property_id"])
    client = D.get_client(conn, deal["client_id"])
    st = D.protection_status(conn, deal["client_id"], deal["property_id"], deal["deal_date"])
    gaps = D.missing_confirmation(conn, d)
    is_party = ctx.actor and ctx.actor["id"] in {c["agent_id"] for c in d["candidates"]}
    is_sup = ctx.actor and ctx.actor["role"] == "supervisor"

    evidence_block = ""
    for c in d["candidates"]:
        evs = []
        for e in c["evidence"]:
            if e.get("viewing_id"):
                evs.append(f"""<div class="evidence small">
                  <div>带看 <a href="/checkin/{e['viewing_id']}">#{e['viewing_id']}</a>
                    {esc(e['appointment_ts'][:16].replace('T',' '))} · 状态：{esc(e['status'])}</div>
                  <div>{('<span class="tag green">客户已确认 '+esc(e['client_confirmed_ts'] or '')+'</span>')
                        if e['client_confirmed'] else '<span class="tag red">缺少客户确认</span>'}
                    {'<span class="tag copper">跨店</span>' if e['cross_store'] else ''}</div>
                  <div class="sig-row">
                    {f'<figure><figcaption>{esc(e["agent_name"])} 签字</figcaption><img src="/{esc(e["sig_agent_path"])}"></figure>' if e['sig_agent_path'] else '<figure><figcaption class=muted>无经纪人签字</figcaption></figure>'}
                    <figure><figcaption>客户签字</figcaption><img src="/{esc(e['sig_client_path'])}"></figure>
                  </div></div>""")
            else:
                evs.append(f'<div class="evidence small muted">{esc(e.get("note","门店份额"))}</div>')
        name = (f'<b>{esc(c["agent"]["name"])}</b><br><span class="small muted">'
                f'{esc(c["store"]["name"])}</span>' if c["agent_id"]
                else f'<b>{esc(c["store"]["name"])}</b><br><span class="small muted">房源方留存</span>')
        share_val = c["suggested_share"]
        decision_share = None
        if "decision" in d:
            decision_share = d["decision"]["outcome"].get(str(c["id"]),
                              d["decision"]["outcome"].get(c["id"]))
        label = D.ROLE_LABELS.get(c["role_label"], c["role_label"])
        evidence_block += f"""<div class="cand">
          <h3>{name} <span class="tag copper">{esc(label)}</span></h3>
          <div class="small">系统建议份额：<b class="money">{pct(share_val)}</b></div>
          {f'<div class="small">裁决份额：<b class="money" style="color:var(--green)">{pct(float(decision_share))}</b></div>' if decision_share is not None else ''}
          <input type="hidden" name="cand_id" value="{c['id']}">
          {''.join(evs)}
        </div>"""

    gaps_html = ""
    if gaps and d["status"] == "open":
        gaps_html = ('<div class="flash err"><b>证据不完整，暂不能裁决：</b>'
                     + "；".join(f'{r}（带看 <a href="/checkin/{e["viewing_id"]}">#{e["viewing_id"]}</a>）'
                                 for r, e in gaps) + "</div>")

    dec_form = ""
    if d["status"] == "open":
        if not ctx.actor:
            dec_form = '<div class="card muted">请在右上角选择主管身份后进行裁决。</div>'
        elif not is_sup:
            dec_form = f'<div class="flash err">当前身份为经纪人，<b>不能自行裁决归属争议</b>，请切换为主管。</div>'
        elif is_party:
            dec_form = '<div class="flash err">该主管是候选经纪人之一（利益相关），不能裁决本争议，请更换非利益相关主管。</div>'
        else:
            share_inputs = "".join(
                f"""<label class="fld">候选 #{c['id']} 最终份额（%）
                  <input class="share-input" type="number" step="1" min="0" max="100"
                  name="share_{c['id']}" value="{round(c['suggested_share']*100)}"></label>"""
                for c in d["candidates"])
            dec_form = f"""<div class="card">
              <h2 style="margin-top:0">主管裁决</h2>
              <form method="post" action="/dispute/{did}/decide">
                <div class="grid3">{share_inputs}</div>
                <label class="fld">裁决依据与说明<textarea name="note" rows="3" required
                  placeholder="须注明核对的规则版本、签到记录与客户确认情况"></textarea></label>
                <label><input type="checkbox" name="evidence_reviewed" value="1" style="width:auto" required>
                  我已核对 <b>规则版本 {esc(d['rule']['version'])}</b>、各候选的<b>签到记录</b>与<b>客户确认</b></label>
                <div style="margin-top:10px"><button type="submit">提交终局裁决</button>
                <span class="small muted" style="margin-left:8px">份额合计必须为 100%；提交后不可更改并写入事件留痕。</span></div>
              </form></div>"""

    decision_html = ""
    if "decision" in d:
        dec = d["decision"]
        decision_html = f"""<div class="decision-banner">
          <h2 style="margin:0 0 6px;border:0">已裁决 · {esc(dec['decided_ts'])}</h2>
          <dl class="kv">
            <dt>裁决主管</dt><dd>{esc(dec['decider']['name'])}（非候选利益相关方）</dd>
            <dt>锁定规则</dt><dd><span class="rule-ver">{esc(d['rule']['version'])}</span> {esc(d['rule']['title'])}</dd>
            <dt>证据核对</dt><dd>{('已核对签到与客户确认' if dec['evidence_reviewed'] else '未核对')}</dd>
            <dt>裁决说明</dt><dd>{esc(dec['note'])}</dd>
          </dl></div>"""

    body = f"""
<h1>佣金归属争议 #{did}</h1>
<div class="sub">{esc(prop['address'])} · 客户 {esc(client['canonical_name'])} · {esc(d['topic'])}</div>
{('<div class="flash err">'+esc(err)+'</div>') if err else ''}
{decision_html}
<div class="card">
  <div class="facts">
    <div class="f"><div class="l">成交日</div><div class="n" style="font-size:15px">{esc(deal['deal_date'])}</div></div>
    <div class="f"><div class="l">成交价</div><div class="n">{money(deal['amount'])}</div></div>
    <div class="f"><div class="l">佣金总额</div><div class="n">{money(deal['commission'])}</div></div>
    <div class="f"><div class="l">规则版本</div><div class="n" style="font-size:15px">
      <span class="rule-ver">{esc(d['rule']['version'])}</span></div></div>
    <div class="f"><div class="l">保护期</div><div class="n" style="font-size:15px">
      {f'<span class="tag green">期内（至 {esc(st["until"])}）</span>' if st['protected'] else '<span class="tag gray">已结束</span>'}</div></div>
  </div>
</div>
{gaps_html}
<h2>候选经纪人证据（并列）<span class="copper">·</span>
  <span class="small muted">建议份额仅作参考，终局以主管裁决为准</span></h2>
<div class="candidates">{evidence_block}</div>
{dec_form}"""
    return layout(ctx, f"争议 #{did}", body, "/disputes")


# ---------------- 规则 / 短信 ----------------

def page_rules(ctx):
    rows = ctx.conn.execute("SELECT * FROM rule_versions ORDER BY effective_from").fetchall()
    cards = []
    for r in rows:
        b = json.loads(r["body_json"])
        cards.append(f"""<div class="card">
          <h2 style="margin-top:0"><span class="rule-ver">{esc(r['version'])}</span> {esc(r['title'])}</h2>
          <dl class="kv">
            <dt>生效日</dt><dd>{esc(r['effective_from'])} 起</dd>
            <dt>保护期</dt><dd>{b['window_days']} 天（首看日起算，含到期日当天）</dd>
            <dt>独家首看</dt><dd>经纪人 {pct(b['sole_first']['viewer_share'])}，房源方门店 {pct(1-b['sole_first']['viewer_share'])}</dd>
            <dt>同日多经纪人</dt><dd>含跨店时门店留存 {pct(b['same_day']['home_store_share_when_cross'])}，余额在首看日各经纪人之间均分</dd>
            <dt>跨店协作</dt><dd>房源方门店 {pct(b['cross_store']['home_store_share'])}，协作经纪人 {pct(1-b['cross_store']['home_store_share'])}</dd>
            <dt>期外/自行到店</dt><dd>佣金 {pct(b['no_protection']['home_store_share'])} 归房源方门店</dd>
          </dl></div>""")
    body = f'<h1>已签署团队规则（版本留痕）</h1><div class="sub">裁决始终锁定成交日生效的版本，规则更新不改写历史。</div>{"".join(cards)}'
    return layout(ctx, "规则版本", body, "/rules")


def page_sms(ctx):
    rows = ctx.conn.execute("SELECT * FROM sms_log ORDER BY id DESC LIMIT 50").fetchall()
    items = ""
    for s in rows:
        from .sms import render
        text = render(s["template"], json.loads(s["vars_json"]))
        items += (f'<tr><td class="small">{esc(s["ts"])}</td><td>{esc(s["phone"])}</td>'
                  f'<td>{esc(text)}</td><td><span class="tag gray">{esc(s["provider"])}</span></td></tr>')
    body = f"""<h1>短信发送记录（模拟适配器）</h1>
<div class="sub">不真正发送短信，只写 sms_log 并打印到服务端控制台。</div>
<div class="card"><table><thead><tr><th>时间</th><th>接收号码</th><th>内容</th><th>通道</th></tr></thead>
<tbody>{items or '<tr><td colspan=4 class=muted>暂无</td></tr>'}</tbody></table></div>"""
    return layout(ctx, "短信", body, "/sms")


# ---------------- HTTP 处理 ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "CopperSeal/1.0"

    def _conn(self):
        return store.get_conn(DB_PATH)

    def _cookie_actor(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        if "actor_id" in c:
            try:
                return int(c["actor_id"].value)
            except ValueError:
                return None
        return None

    def _send(self, code, body, headers=None):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _redirect(self, path):
        self._send(303, "", {"Location": path})

    def _render(self, fn, *args, **kw):
        conn = self._conn()
        try:
            ctx = Ctx(conn, self._cookie_actor(),
                      flash=self.params.pop("_flash", None) if hasattr(self, "params") else None)
            self._send(200, fn(ctx, *args, **kw))
        finally:
            conn.close()

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        self.params = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _action(self, work, ok_redirect, require_actor=True):
        conn = self._conn()
        try:
            actor = self._cookie_actor()
            if require_actor and not actor:
                ctx = Ctx(conn, None, err="请先在右上角选择操作身份。")
                self._send(200, page_home(ctx))
                return
            msg = work(conn, actor)
            self._redirect(ok_redirect + ("&" if "?" in ok_redirect else "?") + "msg="
                           + urllib.parse.quote(msg or "操作成功"))
        except (D.DomainError, D.PermissionError_) as e:
            ctx = Ctx(conn, self._cookie_actor(), err=str(e))
            self._send(200, self.error_page(ctx, str(e)))
        finally:
            conn.close()

    def error_page(self, ctx, msg):
        return page_home(ctx)

    def log_message(self, fmt, *args):
        print("[http] " + fmt % args)

    # GET 路由
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        msg = q.get("msg", [None])[0]
        err = q.get("err", [None])[0]
        conn = self._conn()
        ctx = Ctx(conn, self._cookie_actor(), flash=msg, err=err)
        try:
            if path == "/":
                self._send(200, page_home(ctx))
            elif path == "/properties":
                self._send(200, page_properties(ctx, q.get("q", [""])[0].strip()))
            elif path.startswith("/property/"):
                self._send(200, page_property(ctx, int(path.rsplit("/", 1)[1])))
            elif path == "/clients":
                self._send(200, page_clients(ctx, q.get("q", [""])[0].strip()))
            elif path.startswith("/client/"):
                self._send(200, page_client(ctx, int(path.rsplit("/", 1)[1])))
            elif path == "/calendar":
                self._send(200, page_calendar(ctx, q.get("month", [""])[0],
                                              q.get("store", [""])[0]))
            elif path.startswith("/checkin/"):
                self._send(200, page_checkin(ctx, int(path.rsplit("/", 1)[1])))
            elif path == "/disputes":
                self._send(200, page_disputes(ctx))
            elif path.startswith("/dispute/"):
                self._send(200, page_dispute(ctx, int(path.rsplit("/", 1)[1])))
            elif path == "/rules":
                self._send(200, page_rules(ctx))
            elif path == "/sms":
                self._send(200, page_sms(ctx))
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path.startswith("/signatures/"):
                self._serve_file(os.path.join(SIG_ROOT, path.lstrip("/")))
            else:
                self._send(404, layout(ctx, "404", "<p>页面不存在。</p>"))
        finally:
            conn.close()

    def _serve_static(self, name):
        self._serve_file(os.path.join(STATIC_ROOT, name), ctype="text/css" if name.endswith(".css")
                         else "application/javascript")

    def _serve_file(self, full, ctype="image/png"):
        full = os.path.normpath(full)
        if not full.startswith(SIG_ROOT) and not full.startswith(STATIC_ROOT):
            self._send(403, "forbidden")
            return
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self._send(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # POST 路由
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        self._read_form()
        p = self.params

        if path == "/switch-actor":
            aid = p.get("actor_id", "")
            cookie = f"actor_id={aid}; Path=/; HttpOnly" if aid else 'actor_id=; Path=/; Max-Age=0'
            self._send(303, "", {"Location": "/", "Set-Cookie": cookie})
            return

        if path == "/viewings":
            return self.post_viewing()
        if path == "/deals":
            return self.post_deal()
        if path.startswith("/viewings/") and path.endswith("/cancel"):
            vid = int(path.split("/")[2])
            return self._action(
                lambda conn, actor: self._cancel(conn, actor, vid), f"/property/{p.get('property_id','')}"
                if p.get("property_id") else "/calendar")
        if path.startswith("/viewings/") and path.endswith("/confirm"):
            vid = int(path.split("/")[2])
            return self._action(lambda c, a: self._confirm(c, a, vid), f"/checkin/{vid}")
        if path.startswith("/checkin/"):
            vid = int(path.rsplit("/", 1)[1])
            return self.post_checkin(vid)
        if path.startswith("/dispute/") and path.endswith("/decide"):
            did = int(path.split("/")[2])
            return self.post_decide(did)
        self._send(404, "not found")

    def post_viewing(self):
        p = self.params
        pid = int(p["property_id"])
        when = p["appointment_ts"]

        def work(conn, actor):
            cid = p.get("client_id")
            if not cid:
                name = p.get("new_client_name", "").strip()
                if not name:
                    raise D.DomainError("请选择已有客户或填写新客户姓名")
                c, _ = D.get_or_create_client(
                    conn, name, p.get("new_client_phone", "").strip() or None,
                    p.get("new_client_idno", "").strip() or None,
                    created_store_id=actor_store(conn, actor), actor=actor)
                cid = c["id"]
            is_self = p.get("self_visit") == "1"
            agent_id = None if is_self else int(p.get("agent_id") or actor)
            vid, warn = D.register_viewing(
                conn, int(cid), pid, when, agent_id=agent_id,
                is_self_visit=is_self, cross_store_requested=p.get("cross_confirm") == "1",
                note=p.get("note", ""), actor=actor,
                idem_key=f"{cid}-{pid}-{when}-{agent_id}-{is_self}")
            if warn:
                return (f"带看 #{vid} 已登记。保护期提示：{warn['agent_name']} 的保护期至 "
                        f"{warn['until']}，已通过模拟短信通知本人。")
            return f"带看 #{vid} 已登记"
        self._action(work, f"/property/{pid}")

    def post_deal(self):
        p = self.params
        pid = int(p["property_id"]); cid = int(p["client_id"])

        def work(conn, actor):
            deal_id, result = D.record_deal(
                conn, pid, cid, p["deal_date"], float(p["amount"]),
                float(p["commission"]), actor=actor)
            open_d = conn.execute("SELECT id FROM disputes WHERE deal_id=? AND status='open'",
                                  (deal_id,)).fetchone()
            if open_d:
                raise _Redirect(f"/dispute/{open_d['id']}")
            st = result["status"]
            if not st["protected"]:
                return f"成交已登记：保护期已结束（或无有效带看），佣金归房源方门店。"
            return "成交已登记：独家首看，按规则建议份额分配，无争议。"
        conn = self._conn()
        try:
            actor = self._cookie_actor()
            if not actor:
                self._send(200, layout(Ctx(conn, None, err="请先选择操作身份。"), "提示", ""))
                return
            try:
                msg = work(conn, actor)
                self._redirect(f"/property/{pid}?msg=" + urllib.parse.quote(msg))
            except _Redirect as r:
                self._redirect(r.path + "?msg=" + urllib.parse.quote("成交登记完成，存在多经纪人/跨店候选，已生成争议待主管裁决。"))
            except D.DomainError as e:
                self._send(200, page_property(Ctx(conn, actor, err=str(e)), pid))
        finally:
            conn.close()

    def _cancel(self, conn, actor, vid):
        D.cancel_viewing(conn, vid, reason="页面操作取消", actor=actor)
        return f"带看 #{vid} 已取消；同日可重新登记。"

    def _confirm(self, conn, actor, vid):
        agent = D.get_agent(conn, actor)
        if agent["role"] != "supervisor":
            raise D.PermissionError_("只有主管可以补录客户确认")
        D.record_client_confirmation(conn, vid, actor=actor)
        return f"带看 #{vid} 客户确认已补录"

    def post_checkin(self, vid):
        p = self.params

        def work(conn, actor):
            D.check_in(conn, vid, sig_agent_data=p.get("sig_agent") or None,
                       sig_client_data=p.get("sig_client"),
                       client_confirmed=p.get("client_confirmed") == "1", actor=actor)
            v = conn.execute("SELECT client_id, property_id FROM viewings WHERE id=?", (vid,)).fetchone()
            c = D.get_client(conn, v["client_id"])
            pr = D.get_property(conn, v["property_id"])
            phone = None
            ph = conn.execute(
                "SELECT value FROM client_aliases WHERE client_id=? AND kind='phone' LIMIT 1",
                (v["client_id"],)).fetchone()
            if ph:
                phone = ph["value"]
            send_sms(conn, phone, "checkin_done",
                     {"client": c["canonical_name"], "date": date.today().isoformat(),
                      "property": pr["address"]})
            return f"带看 #{vid} 签到完成，签字证据已保存到本地。"
        conn = self._conn()
        try:
            actor = self._cookie_actor()
            if not actor:
                self._send(200, page_checkin(Ctx(conn, None, err="请先选择操作身份。"), vid))
                return
            try:
                work(conn, actor)
                self._redirect(f"/checkin/{vid}?msg=" + urllib.parse.quote("签到成功"))
            except D.DomainError as e:
                self._send(200, page_checkin(Ctx(conn, actor, err=str(e)), vid, err=str(e)))
        finally:
            conn.close()

    def post_decide(self, did):
        p = self.params
        conn = self._conn()
        try:
            actor = self._cookie_actor()
            if not actor:
                self._send(200, page_dispute(Ctx(conn, None, err="请先选择主管身份。"), did))
                return
            d = D.get_dispute(conn, did)
            outcome = {}
            try:
                for c in d["candidates"]:
                    val = p.get(f"share_{c['id']}")
                    if val == "":
                        raise D.DomainError("请填写全部候选份额")
                    outcome[c["id"]] = float(val) / 100.0
                D.decide_dispute(conn, did, actor, outcome, p.get("note", ""),
                                 p.get("evidence_reviewed") == "1", actor=actor)
            except D.DomainError as e:
                self._send(200, page_dispute(Ctx(conn, actor, err=str(e)), did, err=str(e)))
                return
            # 通知各经纪人（模拟短信）
            d2 = D.get_dispute(conn, did)
            for c in d2["candidates"]:
                if c["agent_id"]:
                    a = D.get_agent(conn, c["agent_id"])
                    send_sms(conn, a["phone"], "dispute_decided",
                             {"agent": a["name"], "dispute_id": did,
                              "rule_version": d2["rule"]["version"]})
            self._redirect(f"/dispute/{did}?msg=" + urllib.parse.quote("裁决已提交并写入事件留痕"))
        finally:
            conn.close()


def actor_store(conn, actor_id):
    a = D.get_agent(conn, actor_id)
    return a["store_id"] if a else None


class _Redirect(Exception):
    def __init__(self, path):
        self.path = path


def main(port=8000):
    store.init_db(DB_PATH)
    if not os.path.exists(DB_PATH):
        raise SystemExit("数据库不存在，请先运行 python -m app.seed")
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"铜印房产业务系统已启动：http://localhost:{port}")
    print("多经纪人冲突样例：首页 → 佣金争议 → #1")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8000)
