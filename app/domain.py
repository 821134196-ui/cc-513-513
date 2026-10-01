"""领域规则：客户去重、带看登记/签到/取消、保护期、佣金候选与争议裁决。

所有写操作同时落一条事件到 events 表，便于留痕与回放。
规则以版本化 JSON 存在 rule_versions，裁决时锁定成交当时生效的版本。
"""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta

from . import db as store

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
SIG_DIR = os.path.join(DATA_DIR, "signatures")


class DomainError(Exception):
    """规则拒绝（含防双记录冲突）。"""


class PermissionError_(DomainError):
    """跨店/角色权限不足。"""


# ---------------- 基础查询 ----------------

def _row(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def get_agent(conn, agent_id):
    return _row(conn, "SELECT * FROM agents WHERE id=?", (agent_id,))


def get_property(conn, property_id):
    return _row(conn, "SELECT * FROM properties WHERE id=?", (property_id,))


def get_client(conn, client_id):
    return _row(conn, "SELECT * FROM clients WHERE id=?", (client_id,))


def get_store(conn, store_id):
    return _row(conn, "SELECT * FROM stores WHERE id=?", (store_id,))


def find_client_by_identity(conn, kind, value):
    """按手机号/证件号/姓名查唯一客户；手机号与证件号命中即视为同一人。"""
    value = (value or "").strip()
    if not value:
        return None
    row = _row(conn, "SELECT client_id FROM client_aliases WHERE kind=? AND value=?",
               (kind, value))
    return get_client(conn, row["client_id"]) if row else None


def find_duplicates(conn, value):
    """检索页去重提示：返回身份标识上可能重复的客户。"""
    value = (value or "").strip()
    if not value:
        return []
    rows = conn.execute(
        """SELECT DISTINCT c.* FROM clients c
           JOIN client_aliases a ON a.client_id=c.id
           WHERE a.value LIKE ? OR c.canonical_name LIKE ?
           ORDER BY c.id""",
        (f"%{value}%", f"%{value}%"),
    ).fetchall()
    return rows


def get_or_create_client(conn, name, phone=None, idno=None, created_store_id=None, actor=None):
    """登记客户：强标识（手机号/证件号）命中既有客户则合并，不另建档案。"""
    name = (name or "").strip()
    for kind, value in (("phone", phone), ("idno", idno)):
        existing = find_client_by_identity(conn, kind, value)
        if existing:
            return existing, False
    existing = find_client_by_identity(conn, "phone", phone) if phone else None
    existing = existing or (find_client_by_identity(conn, "idno", idno) if idno else None)
    if existing:
        return existing, False
    # 姓名不作为自动合并依据（重名常见），仅登记为别名，由检索页提示人工核对
    ts = store.now_iso()
    cur = conn.execute(
        "INSERT INTO clients(canonical_name, created_store_id, created_ts) VALUES (?,?,?)",
        (name, created_store_id, ts),
    )
    cid = cur.lastrowid
    for kind, value in (("phone", phone), ("idno", idno), ("name", name)):
        if value and str(value).strip():
            conn.execute(
                "INSERT INTO client_aliases(client_id, kind, value, created_ts) VALUES (?,?,?,?)",
                (cid, kind, str(value).strip(), ts),
            )
    store.append_event(conn, "client_created", {"client_id": cid, "name": name}, actor)
    return get_client(conn, cid), True


# ---------------- 规则版本 ----------------

def rule_for_date(conn, on_date):
    """成交日生效的规则版本：effective_from <= on_date 中最新的一条。"""
    row = _row(conn,
               "SELECT * FROM rule_versions WHERE effective_from<=? ORDER BY effective_from DESC, id DESC LIMIT 1",
               (on_date,))
    if not row:
        raise DomainError(f"日期 {on_date} 没有已签署生效的团队规则版本")
    return row


def rule_body(rule_row):
    return json.loads(rule_row["body_json"])


# ---------------- 带看登记 ----------------

def _day(ts):
    return ts[:10]


def register_viewing(conn, client_id, property_id, appointment_ts, agent_id=None,
                     is_self_visit=False, cross_store_requested=False, note="",
                     actor=None, idem_key=""):
    """登记带看。并发重复登记由部分唯一索引拒绝，绝不形成双记录。"""
    if not store.claim_idempotency(conn, f"viewing:{idem_key}" if idem_key else ""):
        raise DomainError("重复提交：该带看登记已在处理中")
    prop = get_property(conn, property_id)
    client = get_client(conn, client_id)
    if not prop or not client:
        raise DomainError("房源或客户不存在")

    if is_self_visit:
        agent_id = None
        store_id = prop["store_id"]
        cross_store = 0
    else:
        agent = get_agent(conn, agent_id)
        if not agent or not agent["active"]:
            raise DomainError("经纪人不存在或已停用")
        store_id = agent["store_id"]
        cross_store = 1 if agent["store_id"] != prop["store_id"] else 0
        if cross_store and not agent["can_cross_store"]:
            raise PermissionError_(
                f"经纪人 {agent['name']} 未开通跨门店协作权限，不能登记他店房源带看")
        if cross_store and not cross_store_requested:
            raise PermissionError_("跨门店带看需显式确认协作登记")

    ts = store.now_iso()
    try:
        cur = conn.execute(
            """INSERT INTO viewings(client_id, property_id, agent_id, store_id, appointment_ts,
                  is_self_visit, status, cross_store, note, created_ts)
               VALUES (?,?,?,?,?,?,'scheduled',?,?,?)""",
            (client_id, property_id, agent_id, store_id, appointment_ts,
             1 if is_self_visit else 0, cross_store, note, ts),
        )
    except sqlite3.IntegrityError:
        raise DomainError("该经纪人今天已为同一客户登记过此房源带看（重复登记已拦截）")
    vid = cur.lastrowid
    store.append_event(conn, "viewing_scheduled",
                       {"viewing_id": vid, "client_id": client_id, "property_id": property_id,
                        "agent_id": agent_id, "appointment_ts": appointment_ts,
                        "is_self_visit": bool(is_self_visit), "cross_store": bool(cross_store)},
                       actor)

    # 保护期提示：该客户在此房源上仍处于其他经纪人保护期内 -> 模拟短信提醒原带看人
    warn = protection_warning(conn, client_id, property_id, appointment_ts[:10],
                              exclude_agent=agent_id)
    return vid, warn


def protection_warning(conn, client_id, property_id, on_date, exclude_agent=None):
    st = protection_status(conn, client_id, property_id, on_date)
    holder = None
    if st["protected"]:
        for c in st["basis_viewings"]:
            if c["agent_id"] and c["agent_id"] != exclude_agent:
                holder = c
                break
    if holder:
        agent = get_agent(conn, holder["agent_id"])
        from .sms import send_sms
        send_sms(conn, agent["phone"], "protection_review",
                 {"client": get_client(conn, client_id)["canonical_name"],
                  "property": get_property(conn, property_id)["address"],
                  "date": on_date, "agent": agent["name"], "until": st["until"]})
        return {"agent_id": holder["agent_id"], "agent_name": agent["name"],
                "until": st["until"]}
    return None


def cancel_viewing(conn, viewing_id, reason="", actor=None):
    v = _row(conn, "SELECT * FROM viewings WHERE id=?", (viewing_id,))
    if not v:
        raise DomainError("带看不存在")
    if v["status"] == "cancelled":
        raise DomainError("带看已取消，无需重复操作")
    if v["status"] == "checked_in":
        raise DomainError("已签到的带看不能取消，须由主管发起争议处理")
    conn.execute("UPDATE viewings SET status='cancelled' WHERE id=?", (viewing_id,))
    store.append_event(conn, "viewing_cancelled",
                       {"viewing_id": viewing_id, "reason": reason}, actor)


# ---------------- 签到 / 证据 ----------------

def _save_sig(conn, viewing_id, kind, data_url):
    """把签名 dataURL 落为本地文件，返回相对路径。"""
    if not data_url or "," not in data_url:
        return None
    os.makedirs(SIG_DIR, exist_ok=True)
    import base64
    header, b64 = data_url.split(",", 1)
    ext = "png" if "png" in header else "jpg"
    path = os.path.join(SIG_DIR, f"v{viewing_id}_{kind}.{ext}")
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    rel = f"signatures/v{viewing_id}_{kind}.{ext}"
    return rel


def check_in(conn, viewing_id, sig_agent_data=None, sig_client_data=None,
             client_confirmed=False, actor=None):
    v = _row(conn, "SELECT * FROM viewings WHERE id=?", (viewing_id,))
    if not v:
        raise DomainError("带看不存在")
    if v["status"] == "cancelled":
        raise DomainError("带看已取消，不能签到")
    if v["status"] == "checked_in":
        raise DomainError("该带看已签到，不能重复签到")
    if not v["is_self_visit"] and not sig_agent_data:
        raise DomainError("缺少经纪人签字，无法完成签到")
    if not sig_client_data:
        raise DomainError("缺少客户签字，无法完成签到")
    ts = store.now_iso()
    agent_path = _save_sig(conn, viewing_id, "agent", sig_agent_data)
    client_path = _save_sig(conn, viewing_id, "client", sig_client_data)
    conn.execute(
        """UPDATE viewings SET status='checked_in', checkin_ts=?,
               sig_agent_path=?, sig_client_path=?,
               client_confirmed=?, client_confirmed_ts=?
           WHERE id=?""",
        (ts, agent_path, client_path, 1 if client_confirmed else 0,
         ts if client_confirmed else None, viewing_id),
    )
    store.append_event(conn, "viewing_checked_in",
                       {"viewing_id": viewing_id, "client_confirmed": client_confirmed,
                        "sig_agent": agent_path, "sig_client": client_path}, actor)


def record_client_confirmation(conn, viewing_id, actor=None):
    """补录客户确认（主管核对电话/现场确认），仍缺确认的候选不可裁决。"""
    v = _row(conn, "SELECT * FROM viewings WHERE id=?", (viewing_id,))
    if not v:
        raise DomainError("带看不存在")
    if v["status"] != "checked_in":
        raise DomainError("仅已签到带看可记录客户确认")
    if v["client_confirmed"]:
        return
    ts = store.now_iso()
    conn.execute("UPDATE viewings SET client_confirmed=1, client_confirmed_ts=? WHERE id=?",
                 (ts, viewing_id))
    store.append_event(conn, "client_confirmed", {"viewing_id": viewing_id}, actor)


# ---------------- 保护期 ----------------

def valid_viewings(conn, client_id, property_id):
    """成交资格证据：已签到、未取消、经纪人带看（自行到店单列）。"""
    rows = conn.execute(
        """SELECT * FROM viewings
           WHERE client_id=? AND property_id=? AND status='checked_in'
           ORDER BY appointment_ts, id""",
        (client_id, property_id),
    ).fetchall()
    return rows


def protection_status(conn, client_id, property_id, on_date):
    """返回 on_date 当天保护期状态、首带看、到期日与依据带看。

    保护期窗口在首看发生时即对经纪人确立，因此窗口天数取“首看日”生效的规则版本，
    后续签署的新版本不会延长或缩短既有保护期；分成比例另按成交日规则计算。
    """
    checked = [v for v in valid_viewings(conn, client_id, property_id)
               if _day(v["appointment_ts"]) <= on_date]
    agent_checked = [v for v in checked if v["agent_id"] is not None]
    if not agent_checked:
        # 只有客户自行到店（或没有带看）：不产生任何经纪人保护期
        rule_now = None
        try:
            rule_now = rule_for_date(conn, on_date)
        except DomainError:
            rule_now = None
        return {"protected": False,
                "window_days": rule_body(rule_now)["window_days"] if rule_now else None,
                "rule_version": rule_now["version"] if rule_now else None,
                "rule_version_id": rule_now["id"] if rule_now else None,
                "basis_viewings": checked, "first_date": None, "until": None}
    first_date = min(_day(v["appointment_ts"]) for v in agent_checked)
    rule = rule_for_date(conn, first_date)
    window = rule_body(rule)["window_days"]
    until = (datetime.strptime(first_date, "%Y-%m-%d").date() + timedelta(days=window)) \
        .isoformat()
    # 保护期含到期日当天；until 次日起失效
    protected = on_date <= until
    return {"protected": protected, "window_days": window, "rule_version": rule["version"],
            "rule_version_id": rule["id"], "basis_viewings": checked,
            "first_date": first_date, "until": until}


# ---------------- 成交与佣金候选 ----------------

def commission_candidates(conn, deal_row):
    """按成交日生效的团队规则计算归属候选（只给建议份额，最终由主管裁决）。"""
    prop = get_property(conn, deal_row["property_id"])
    rule = rule_for_date(conn, deal_row["deal_date"])
    body = rule_body(rule)
    st = protection_status(conn, deal_row["client_id"], deal_row["property_id"],
                           deal_row["deal_date"])
    checked = st["basis_viewings"]
    agent_views = [v for v in checked if v["agent_id"] is not None]
    home = {"store_id": prop["store_id"], "role_label": "home_store", "agent_id": None,
            "viewing_ids": [], "suggested_share": 0.0, "cross": False}

    if not st["protected"] or not agent_views:
        # 保护期外成交 / 只有客户自行到店：佣金归房源方门店
        home["suggested_share"] = body["no_protection"]["home_store_share"]
        home["role_label"] = "self_visit" if agent_views == [] and any(
            v["is_self_visit"] for v in checked) else "no_protection"
        return {"rule": rule, "status": st, "candidates": [home], "needs_dispute": False}

    first_date = st["first_date"]
    first_day = [v for v in agent_views if _day(v["appointment_ts"]) == first_date]
    agents = {}
    for v in first_day:
        agents.setdefault(v["agent_id"], []).append(v)
    any_cross = any(v["store_id"] != prop["store_id"] for v in first_day)

    candidates = []
    if len(agents) == 1 and not any_cross:
        aid, views = next(iter(agents.items()))
        share = body["sole_first"]["viewer_share"]
        home["suggested_share"] = round(1.0 - share, 4)
        candidates.append(home)
        candidates.append(_party(aid, views, "first", share))
    elif len(agents) == 1 and any_cross:
        aid, views = next(iter(agents.items()))
        home_share = body["cross_store"]["home_store_share"]
        home["suggested_share"] = home_share
        candidates.append(home)
        candidates.append(_party(aid, views, "cross_store", 1.0 - home_share, cross=True))
    else:
        # 同一天多名经纪人（含跨店混合）：门店留存后余额均分
        home_share = body["same_day"].get("home_store_share_when_cross", 0.2) if any_cross else 0.0
        pool = 1.0 - home_share
        each = round(pool / len(agents), 4)
        if home_share:
            home["suggested_share"] = round(1.0 - each * len(agents), 4)
            candidates.append(home)
        for aid, views in agents.items():
            cross = views[0]["store_id"] != prop["store_id"]
            candidates.append(_party(aid, views, "same_day" if not cross else "cross_store",
                                     each, cross=cross))
    ambiguous = len([c for c in candidates if c["agent_id"] is not None]) > 1 or any_cross
    return {"rule": rule, "status": st, "candidates": candidates,
            "needs_dispute": ambiguous}


def _party(agent_id, views, label, share, cross=False):
    return {"agent_id": agent_id, "store_id": views[0]["store_id"], "role_label": label,
            "viewing_ids": [v["id"] for v in views],
            "suggested_share": round(share, 4), "cross": cross}


def _evidence_snapshot(conn, viewing_ids):
    snap = []
    for vid in viewing_ids:
        v = _row(conn, "SELECT * FROM viewings WHERE id=?", (vid,))
        a = get_agent(conn, v["agent_id"]) if v["agent_id"] else None
        snap.append({"viewing_id": vid, "appointment_ts": v["appointment_ts"],
                     "checkin_ts": v["checkin_ts"], "status": v["status"],
                     "agent_name": a["name"] if a else "客户自行到店",
                     "sig_agent_path": v["sig_agent_path"],
                     "sig_client_path": v["sig_client_path"],
                     "client_confirmed": bool(v["client_confirmed"]),
                     "client_confirmed_ts": v["client_confirmed_ts"],
                     "cross_store": bool(v["cross_store"])})
    return snap


def record_deal(conn, property_id, client_id, deal_date, amount, commission,
                actor=None, auto_open_dispute=True):
    rule = rule_for_date(conn, deal_date)
    ts = store.now_iso()
    st = protection_status(conn, client_id, property_id, deal_date)
    cur = conn.execute(
        """INSERT INTO deals(property_id, client_id, deal_date, amount, commission,
              rule_version_id, protection_active, recorded_ts)
           VALUES (?,?,?,?,?,?,?,?)""",
        (property_id, client_id, deal_date, amount, commission, rule["id"],
         1 if st["protected"] else 0, ts),
    )
    deal_id = cur.lastrowid
    conn.execute("UPDATE properties SET status='dealt' WHERE id=?", (property_id,))
    result = commission_candidates(conn, _row(conn, "SELECT * FROM deals WHERE id=?", (deal_id,)))
    store.append_event(conn, "deal_recorded",
                       {"deal_id": deal_id, "property_id": property_id,
                        "client_id": client_id, "deal_date": deal_date,
                        "commission": commission, "rule_version": rule["version"],
                        "protection_active": st["protected"],
                        "candidates": result["candidates"]}, actor)
    if auto_open_dispute and st["protected"] and result["needs_dispute"]:
        open_dispute(conn, deal_id, "多经纪人/跨店归属，待主管裁决", actor=actor)
    return deal_id, result


# ---------------- 争议 ----------------

def open_dispute(conn, deal_id, topic, actor=None):
    deal = _row(conn, "SELECT * FROM deals WHERE id=?", (deal_id,))
    if not deal:
        raise DomainError("成交不存在")
    existing = _row(conn, "SELECT id FROM disputes WHERE deal_id=? AND status='open'", (deal_id,))
    if existing:
        return existing["id"]
    result = commission_candidates(conn, deal)
    rule = result["rule"]
    ts = store.now_iso()
    cur = conn.execute(
        """INSERT INTO disputes(deal_id, topic, status, rule_version_id, window_days,
              opened_by, opened_ts) VALUES (?,?,'open',?,?,?,?)""",
        (deal_id, topic, rule["id"], result["status"]["window_days"],
         actor if isinstance(actor, int) else None, ts),
    )
    dispute_id = cur.lastrowid
    for c in result["candidates"]:
        vids = c["viewing_ids"]
        conn.execute(
            """INSERT INTO dispute_candidates(dispute_id, agent_id, store_id, role_label,
                  suggested_share, viewing_ids, evidence_json)
               VALUES (?,?,?,?,?,?,?)""",
            (dispute_id, c["agent_id"], c["store_id"], c["role_label"],
             c["suggested_share"], json.dumps(vids),
             json.dumps(_evidence_snapshot(conn, vids) if vids else
                        [{"note": "门店留存份额，无带看证据"}], ensure_ascii=False)),
        )
    store.append_event(conn, "dispute_opened",
                       {"dispute_id": dispute_id, "deal_id": deal_id, "topic": topic,
                        "rule_version": rule["version"],
                        "candidates": result["candidates"]}, actor)
    return dispute_id


def get_dispute(conn, dispute_id):
    d = _row(conn, "SELECT * FROM disputes WHERE id=?", (dispute_id,))
    if not d:
        return None
    deal = _row(conn, "SELECT * FROM deals WHERE id=?", (d["deal_id"],))
    rule = _row(conn, "SELECT * FROM rule_versions WHERE id=?", (d["rule_version_id"],))
    cands = conn.execute(
        "SELECT * FROM dispute_candidates WHERE dispute_id=? ORDER BY id", (dispute_id,)).fetchall()
    out = dict(d)
    out["deal"] = dict(deal)
    out["rule"] = dict(rule)
    parties = []
    for c in cands:
        cd = dict(c)
        cd["viewing_ids"] = json.loads(c["viewing_ids"])
        # 证据以实时带看记录为准（主管补录客户确认后立即反映），JSON 快照仅作开案留档
        cd["evidence"] = (_evidence_snapshot(conn, cd["viewing_ids"])
                          if cd["viewing_ids"] else json.loads(c["evidence_json"]))
        if c["agent_id"]:
            cd["agent"] = dict(get_agent(conn, c["agent_id"]))
        cd["store"] = dict(get_store(conn, c["store_id"]))
        parties.append(cd)
    out["candidates"] = parties
    dec = _row(conn, "SELECT * FROM dispute_decisions WHERE dispute_id=?", (dispute_id,))
    if dec:
        dd = dict(dec)
        dd["outcome"] = json.loads(dd["outcome_json"])
        dd["decider"] = dict(get_agent(conn, dec["decided_by"]))
        out["decision"] = dd
    return out


def missing_confirmation(conn, dispute):
    """裁决前检查：候选证据中的带看必须已签到且客户已确认。"""
    missing = []
    for c in dispute["candidates"]:
        for e in c["evidence"]:
            if e.get("viewing_id"):
                if e["status"] != "checked_in":
                    missing.append(("缺少签到", e))
                if not e["client_confirmed"]:
                    missing.append(("缺少客户确认", e))
    return missing


ROLE_LABELS = {"first": "首看人", "same_day": "同日带看", "cross_store": "跨店协作带看",
               "self_visit": "客户自行到店", "home_store": "房源方门店",
               "no_protection": "保护期外成交"}


def decide_dispute(conn, dispute_id, decided_by, outcome, note, evidence_reviewed, actor=None):
    """主管裁决。约束：主管角色、非利益相关方、锁定规则版本、证据齐全、份额合计 100%。"""
    dispute = get_dispute(conn, dispute_id)
    if not dispute:
        raise DomainError("争议不存在")
    if dispute["status"] != "open":
        raise DomainError("该争议已裁决")
    decider = get_agent(conn, decided_by)
    if not decider or decider["role"] != "supervisor":
        raise PermissionError_("只有主管可以裁决佣金归属争议")
    party_ids = {c["agent_id"] for c in dispute["candidates"] if c["agent_id"]}
    if decided_by in party_ids:
        raise PermissionError_("经纪人不能裁决本人的归属争议，请由非利益相关主管处理")
    if not evidence_reviewed:
        raise DomainError("裁决前必须确认已核对规则版本、签到记录与客户确认")
    gaps = missing_confirmation(conn, dispute)
    if gaps:
        raise DomainError("证据不完整，仍有候选缺少签到或客户确认："
                          + "；".join(f"{reason}(带看#{e['viewing_id']})" for reason, e in gaps))

    cand_ids = {c["id"] for c in dispute["candidates"]}
    total = 0.0
    norm = {}
    for cid, share in outcome.items():
        cid = int(cid)
        if cid not in cand_ids:
            raise DomainError("裁决对象超出候选范围")
        share = float(share)
        if share < 0 or share > 1:
            raise DomainError("份额必须在 0~1 之间")
        total += share
        norm[cid] = share
    if abs(total - 1.0) > 1e-6:
        raise DomainError(f"最终份额合计必须为 100%，当前为 {total*100:.1f}%")

    ts = store.now_iso()
    conn.execute(
        """INSERT INTO dispute_decisions(dispute_id, decided_by, decided_ts, rule_version_id,
              outcome_json, note, evidence_reviewed)
           VALUES (?,?,?,?,?,?,1)""",
        (dispute_id, decided_by, ts, dispute["rule_version_id"],
         json.dumps(norm, ensure_ascii=False), note),
    )
    conn.execute("UPDATE disputes SET status='decided', decided_ts=? WHERE id=?",
                 (ts, dispute_id))
    store.append_event(conn, "dispute_decided",
                       {"dispute_id": dispute_id, "decided_by": decided_by,
                        "rule_version": dispute["rule"]["version"],
                        "outcome": norm, "note": note,
                        "evidence_reviewed": True}, actor or decided_by)
    return get_dispute(conn, dispute_id)


# ---------------- 检索 / 日历 ----------------

def search_properties(conn, q="", store_id=None):
    sql = """SELECT p.*, s.name AS store_name FROM properties p
             JOIN stores s ON s.id=p.store_id WHERE 1=1"""
    args = []
    if q:
        sql += " AND (p.address LIKE ? OR p.district LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if store_id:
        sql += " AND p.store_id=?"
        args.append(store_id)
    return conn.execute(sql + " ORDER BY p.id DESC", args).fetchall()


def search_clients(conn, q=""):
    if not q:
        return conn.execute(
            """SELECT c.*, GROUP_CONCAT(a.kind||':'||a.value, ' / ') AS identities
               FROM clients c LEFT JOIN client_aliases a ON a.client_id=c.id
               GROUP BY c.id ORDER BY c.id DESC LIMIT 50""").fetchall()
    return conn.execute(
        """SELECT c.*, GROUP_CONCAT(a.kind||':'||a.value, ' / ') AS identities
           FROM clients c LEFT JOIN client_aliases a ON a.client_id=c.id
           WHERE c.id IN (SELECT client_id FROM client_aliases WHERE value LIKE ?)
              OR c.canonical_name LIKE ?
           GROUP BY c.id ORDER BY c.id""",
        (f"%{q}%", f"%{q}%")).fetchall()


def calendar_viewings(conn, day_from, day_to, store_id=None):
    sql = """SELECT v.*, p.address, c.canonical_name AS client_name,
                    a.name AS agent_name, s.name AS store_name
             FROM viewings v
             JOIN properties p ON p.id=v.property_id
             JOIN clients c ON c.id=v.client_id
             LEFT JOIN agents a ON a.id=v.agent_id
             JOIN stores s ON s.id=v.store_id
             WHERE date(v.appointment_ts) BETWEEN ? AND ?"""
    args = [day_from, day_to]
    if store_id:
        sql += " AND v.store_id=?"
        args.append(store_id)
    return conn.execute(sql + " ORDER BY v.appointment_ts", args).fetchall()


def property_timeline(conn, property_id):
    return conn.execute(
        """SELECT v.*, c.canonical_name AS client_name, a.name AS agent_name,
                  s.name AS store_name
           FROM viewings v
           JOIN clients c ON c.id=v.client_id
           LEFT JOIN agents a ON a.id=v.agent_id
           JOIN stores s ON s.id=v.store_id
           WHERE v.property_id=? ORDER BY v.appointment_ts, v.id""",
        (property_id,)).fetchall()
