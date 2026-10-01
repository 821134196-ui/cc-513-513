"""领域服务：所有业务判定集中于此。

规则版本：rule_versions 中保存签署版全文（JSON），任何带看与成交都冻结当时
版本号；裁决时必须能回放该版本。

并发：Database 提供线程局部连接；写命令在 BEGIN IMMEDIATE 事务内完成
「校验 + 追加事件 + 更新投影」，SQLite 写锁使其全局串行，
ux_viewing_slot 唯一索引是最后防线——并发登记同一带看只可能成功一条。
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta

from .db import Database
from .sms_adapter import SmsAdapter
from .timeutil import (confirm_code, d, is_protected, new_id,
                       now, now_ts, parse_appt)

WALKIN = "WALKIN"


class DomainError(Exception):
    """可直接展示给用户的业务拒绝。"""


class ConflictError(DomainError):
    """并发/唯一约束冲突（未产生重复记录）。"""


def _norm_name(name: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKC", name or "") if not ch.isspace()
    )


class Service:
    def __init__(self, database: Database | sqlite3.Connection,
                 sms: SmsAdapter | None = None):
        if isinstance(database, sqlite3.Connection):
            # 兼容测试直接传连接：包一个临时 Database（不跨线程共享）
            self.db = _ConnHolder(database)
        else:
            self.db = database
        self.sms = sms or SmsAdapter(self.conn)

    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.conn()

    @contextmanager
    def _tx(self):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------------ utils
    def _emit(self, stream_type: str, stream_id: str, etype: str,
              data: dict, actor: str = "", rule_version: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events(ts,stream_type,stream_id,type,actor,rule_version,data)"
            " VALUES(?,?,?,?,?,?,?)",
            (now_ts(), stream_type, stream_id, etype, actor, rule_version,
             json.dumps(data, ensure_ascii=False)),
        )

    def _deny(self, actor: str, action: str, reason: str, detail: dict) -> None:
        # 拒绝动作独立落库（autocommit），即使后续业务事务回滚也保留留痕
        self.conn.execute(
            "INSERT INTO denied_actions VALUES(?,?,?,?,?,?)",
            (new_id("deny"), now_ts(), actor, action, reason,
             json.dumps(detail, ensure_ascii=False)),
        )

    def row(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchone()

    def rows(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------- 规则版本
    def publish_rule(self, version: str, body: dict, text: str,
                     published_by: str, activate: bool = True) -> dict:
        body = dict(body)
        body["text"] = text
        ts = now_ts()
        with self._tx():
            if self.row("SELECT 1 FROM rule_versions WHERE version=?", (version,)):
                raise DomainError(f"规则版本 {version} 已存在")
            self.conn.execute(
                "INSERT INTO rule_versions(version,body,published_at,published_by,is_active)"
                " VALUES(?,?,?,?,?)",
                (version, json.dumps(body, ensure_ascii=False), ts, published_by, 0))
            if activate:
                self.conn.execute("UPDATE rule_versions SET is_active=0")
                self.conn.execute(
                    "UPDATE rule_versions SET is_active=1 WHERE version=?", (version,))
            self._emit("rule", version, "rule.published",
                       {"version": version, "body": body}, actor=published_by)
        return self.get_rule(version)

    def get_rule(self, version: str) -> dict:
        r = self.row("SELECT * FROM rule_versions WHERE version=?", (version,))
        if not r:
            raise DomainError(f"规则版本不存在: {version}")
        body = json.loads(r["body"])
        return {"version": r["version"], "body": body, "text": body.get("text", ""),
                "published_at": r["published_at"], "published_by": r["published_by"],
                "is_active": bool(r["is_active"])}

    def active_rule(self) -> dict:
        r = self.row("SELECT version FROM rule_versions WHERE is_active=1")
        if not r:
            raise DomainError("尚无已签署生效的团队规则版本")
        return self.get_rule(r["version"])

    def list_rules(self) -> list[dict]:
        return [self.get_rule(r["version"])
                for r in self.rows("SELECT version FROM rule_versions ORDER BY published_at")]

    # ---------------------------------------------------------------- 门店/人
    def add_store(self, store_id: str, name: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO stores VALUES(?,?)", (store_id, name))

    def add_agent(self, agent_id: str, name: str, store_id: str,
                  role: str = "agent", phone: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO agents(agent_id,name,phone,store_id,role)"
            " VALUES(?,?,?,?,?)", (agent_id, name, phone, store_id, role))

    def get_agent(self, agent_id: str):
        if agent_id == WALKIN:
            return {"agent_id": WALKIN, "name": "客户自行到店", "store_id": None,
                    "role": "walkin"}
        r = self.row("SELECT * FROM agents WHERE agent_id=?", (agent_id,))
        return dict(r) if r else None

    # ------------------------------------------------------------------ 房源
    def add_property(self, property_id: str, address: str, store_id: str,
                     price_cents: int, layout: str = "", area_sqm: float = 0,
                     listed_at: str = "") -> dict:
        with self._tx():
            self.conn.execute(
                "INSERT INTO properties(property_id,address,layout,area_sqm,price_cents,"
                "store_id,status,listed_at) VALUES(?,?,?,?,?,?,'在售',?)",
                (property_id, address, layout, area_sqm, price_cents, store_id,
                 listed_at or d(now())))
            self._emit("property", property_id, "property.listed",
                       {"address": address, "store_id": store_id,
                        "price_cents": price_cents})
        return self.get_property(property_id)

    def get_property(self, pid: str) -> dict:
        r = self.row("SELECT * FROM properties WHERE property_id=?", (pid,))
        if not r:
            raise DomainError("房源不存在")
        p = dict(r)
        p["store_name"] = self.row(
            "SELECT name FROM stores WHERE store_id=?", (p["store_id"],))["name"]
        return p

    def search_properties(self, q: str = "") -> list[dict]:
        q = (q or "").strip()
        if q:
            rows = self.rows(
                "SELECT * FROM properties WHERE address LIKE ? ORDER BY listed_at DESC",
                (f"%{q}%",))
        else:
            rows = self.rows(
                "SELECT * FROM properties ORDER BY listed_at DESC, property_id")
        out = []
        for r in rows:
            p = dict(r)
            s = self.row("SELECT name FROM stores WHERE store_id=?", (p["store_id"],))
            p["store_name"] = s["name"] if s else p["store_id"]
            out.append(p)
        return out

    # ------------------------------------------------------------------ 客户
    def find_duplicates(self, name: str, phone: str) -> list[dict]:
        """精确：手机号一致。疑似：姓名归一化相同且手机后四位相同。"""
        exact = self.rows("SELECT * FROM customers WHERE phone=? AND merged_into IS NULL",
                          (phone,))
        if exact:
            return [dict(r) | {"dup_type": "exact"} for r in exact]
        tail = phone[-4:] if phone else ""
        if tail and name:
            nn = _norm_name(name)
            near = []
            for r in self.rows(
                    "SELECT * FROM customers WHERE merged_into IS NULL"):
                if r["phone"][-4:] == tail and _norm_name(r["name"]) == nn:
                    near.append(dict(r) | {"dup_type": "suspected"})
            return near
        return []

    def register_customer(self, name: str, phone: str, source_agent_id: str = "",
                          note: str = "", force: bool = False) -> dict:
        phone = (phone or "").strip().replace(" ", "")
        if not name or not phone:
            raise DomainError("客户姓名与手机号必填")
        dupes = self.find_duplicates(name, phone)
        exact = [x for x in dupes if x["dup_type"] == "exact"]
        if exact:
            self._deny(source_agent_id, "customer.register", "手机号已存在（重复客户）",
                       {"phone": phone, "existing": exact[0]["customer_id"]})
            raise DomainError(
                f"重复客户：手机号 {phone} 已登记为「{exact[0]['name']}」"
                f"（{exact[0]['customer_id']}），请直接使用既有档案或发起合并")
        suspected = [x for x in dupes if x["dup_type"] == "suspected"]
        if suspected and not force:
            self._deny(source_agent_id, "customer.register", "疑似重复客户，待人工确认",
                       {"phone": phone, "candidates": [s["customer_id"] for s in suspected]})
            raise DomainError(
                f"疑似重复客户：已有「{suspected[0]['name']}」尾号{suspected[0]['phone'][-4:]}"
                "。核对后请使用 force=1 确认确为不同人，或走客户合并")
        cid = new_id("cust")
        ts = now_ts()
        with self._tx():
            self.conn.execute(
                "INSERT INTO customers(customer_id,name,phone,source_agent_id,"
                "first_registered_at,note) VALUES(?,?,?,?,?,?)",
                (cid, name, phone, source_agent_id or None, ts, note))
            self._emit("customer", cid, "customer.registered",
                       {"name": name, "phone": phone, "source_agent_id": source_agent_id,
                        "force": force or bool(suspected)},
                       actor=source_agent_id)
        return self.get_customer(cid)

    def get_customer(self, cid: str) -> dict:
        r = self.row("SELECT * FROM customers WHERE customer_id=?", (cid,))
        if not r:
            raise DomainError("客户不存在")
        c = dict(r)
        if c["merged_into"]:
            c["merged_into_customer"] = self.get_customer(c["merged_into"])
        return c

    def search_customers(self, q: str = "") -> list[dict]:
        q = (q or "").strip()
        if q:
            rows = self.rows(
                "SELECT * FROM customers WHERE name LIKE ? OR phone LIKE ?"
                " ORDER BY first_registered_at DESC",
                (f"%{q}%", f"%{q}%"))
        else:
            rows = self.rows("SELECT * FROM customers ORDER BY first_registered_at DESC")
        return [dict(r) for r in rows]

    def merge_customers(self, keep_id: str, drop_id: str, actor: str) -> dict:
        keep, drop = self.get_customer(keep_id), self.get_customer(drop_id)
        with self._tx():
            self.conn.execute(
                "UPDATE viewings SET customer_id=? WHERE customer_id=?",
                (keep_id, drop_id))
            self.conn.execute(
                "UPDATE customers SET merged_into=? WHERE customer_id=?",
                (keep_id, drop_id))
            self._emit("customer", keep_id, "customer.merged",
                       {"kept": keep_id, "merged_away": drop_id,
                        "kept_name": keep["name"], "drop_name": drop["name"]},
                       actor=actor)
        return self.get_customer(keep_id)

    # ------------------------------------------------------------- 跨店协作
    def grant_collab(self, property_id: str, agent_id: str, granted_by: str) -> dict:
        prop = self.get_property(property_id)
        agent = self.get_agent(agent_id)
        if not agent or agent["agent_id"] == WALKIN:
            raise DomainError("被授权经纪人不存在")
        granter = self.get_agent(granted_by)
        if not granter or granter["role"] != "supervisor":
            raise DomainError("只有主管可以签发跨店协作授权")
        if granter["store_id"] != prop["store_id"]:
            raise DomainError("跨店协作须由房源方门店主管授权")
        if agent["store_id"] == prop["store_id"]:
            raise DomainError("经纪人与房源同店，无需跨店授权")
        gid = new_id("grant")
        with self._tx():
            self.conn.execute(
                "INSERT INTO collab_grants VALUES(?,?,?,?,?,0)",
                (gid, property_id, agent_id, granted_by, now_ts()))
            self._emit("property", property_id, "collab.granted",
                       {"agent_id": agent_id, "granted_by": granted_by}, actor=granted_by)
        return {"grant_id": gid, "property_id": property_id, "agent_id": agent_id}

    def _can_schedule(self, prop: dict, agent_id: str) -> tuple[bool, str]:
        if agent_id == WALKIN:
            return True, "walkin"  # 到店登记任何门店可记录事实，但不产生经纪人归属
        agent = self.get_agent(agent_id)
        if not agent:
            return False, "经纪人不存在"
        if agent["store_id"] == prop["store_id"]:
            return True, "same_store"
        g = self.row(
            "SELECT 1 FROM collab_grants WHERE property_id=? AND agent_id=?"
            " AND revoked=0", (prop["property_id"], agent_id))
        return (True, "collab") if g else (False, "未取得房源方门店的跨店协作授权")

    # ------------------------------------------------------------------ 带看
    def schedule_viewing(self, property_id: str, customer_id: str, agent_id: str,
                         appointment_time: str, ide_key: str = "",
                         store_id_override: str = "") -> dict:
        prop = self.get_property(property_id)
        customer = self.get_customer(customer_id)
        appt = parse_appt(appointment_time)
        rule = self.active_rule()

        if ide_key:
            hit = self.row("SELECT result FROM idempotency WHERE ide_key=?",
                           (f"view:{ide_key}",))
            if hit:
                return json.loads(hit["result"])

        appt_s = appt.strftime("%Y-%m-%d %H:%M:%S")
        # 权限校验放在事务外：拒绝动作要独立留痕，不随业务事务回滚
        ok, why = self._can_schedule(prop, agent_id)
        if not ok:
            self._deny(agent_id, "viewing.schedule", why,
                       {"property_id": property_id, "store_id": prop["store_id"]})
            raise DomainError(f"跨店登记被拒绝：{why}")

        with self._tx() as conn:
            clash = conn.execute(
                "SELECT viewing_id FROM viewings WHERE property_id=? AND customer_id=?"
                " AND appointment_time=? AND status<>'cancelled'",
                (property_id, customer_id, appt_s)).fetchone()
            if clash:
                raise ConflictError(
                    "该客户在该房源此时段已有带看记录（可能为并发登记），"
                    "系统未生成重复记录")

            vid = new_id("view")
            store_id = (agent_id != WALKIN and self.get_agent(agent_id)["store_id"]
                        ) or store_id_override or prop["store_id"]
            collab = 1 if why == "collab" else 0
            code = confirm_code()
            conn.execute(
                "INSERT INTO viewings(viewing_id,property_id,customer_id,agent_id,"
                "store_id,appointment_time,status,collab,rule_version,created_at,"
                "confirm_code) VALUES(?,?,?,?,?,?,'scheduled',?,?,?,?)",
                (vid, property_id, customer_id, agent_id, store_id,
                 appt_s, collab, rule["version"], now_ts(), code))
            self._emit("viewing", vid, "viewing.scheduled",
                       {"property_id": property_id, "customer_id": customer_id,
                        "agent_id": agent_id, "store_id": store_id,
                        "appointment_time": appt_s,
                        "collab": collab, "rule_version": rule["version"]},
                       actor=agent_id, rule_version=rule["version"])
            result = self.get_viewing(vid)
            if ide_key:
                conn.execute("INSERT OR IGNORE INTO idempotency VALUES(?,?,?)",
                             (f"view:{ide_key}",
                              json.dumps(result, ensure_ascii=False), now_ts()))

        # 事务提交后再发短信
        if customer.get("phone"):
            self.sms.send(
                customer["phone"],
                f"【{prop['store_name']}】您预约 {d(appt)} 查看「{prop['address']}」"
                f"已登记。到店请出示确认码 {code}。",
                "viewing", vid)
            with self._tx():
                self.conn.execute(
                    "UPDATE viewings SET sms_sent=1 WHERE viewing_id=?", (vid,))
        return self.get_viewing(vid)

    def cancel_viewing(self, viewing_id: str, actor: str, reason: str,
                       cancelled_at: str = "") -> dict:
        v = self.get_viewing(viewing_id)
        if v["status"] == "cancelled":
            raise DomainError("带看已取消")
        with self._tx():
            self.conn.execute(
                "UPDATE viewings SET status='cancelled', cancelled_at=?, cancel_reason=?"
                " WHERE viewing_id=?", (cancelled_at or now_ts(), reason, viewing_id))
            self._emit("viewing", viewing_id, "viewing.cancelled",
                       {"reason": reason, "previous_status": v["status"]}, actor=actor,
                       rule_version=v["rule_version"])
        return self.get_viewing(viewing_id)

    def check_in(self, viewing_id: str, lat: float, lng: float,
                 sign_path: str, actor: str, checked_at: str = "") -> dict:
        v = self.get_viewing(viewing_id)
        if v["status"] == "cancelled":
            raise DomainError("带看已取消，不能签到")
        if v["status"] == "checked":
            raise DomainError("已签到，请勿重复签到")
        if actor != WALKIN and actor != v["agent_id"]:
            agent = self.get_agent(actor)
            if not agent or agent["role"] != "supervisor":
                raise DomainError("只能由带看经纪人本人或主管签到")
        if not sign_path:
            raise DomainError("签到必须保存客户签字图")
        ts = checked_at or now_ts()
        with self._tx():
            self.conn.execute(
                "UPDATE viewings SET status='checked', checked_in_at=?, lat=?, lng=?,"
                " sign_path=? WHERE viewing_id=?",
                (ts, lat, lng, sign_path, viewing_id))
            self._emit("viewing", viewing_id, "viewing.checked_in",
                       {"checked_in_at": ts, "lat": lat, "lng": lng,
                        "sign_path": sign_path}, actor=actor,
                       rule_version=v["rule_version"])
        v = self.get_viewing(viewing_id)
        c = self.get_customer(v["customer_id"])
        rule = self.get_rule(v["rule_version"])
        self.sms.send(
            c["phone"],
            f"【带看确认】您于 {ts[:16]} 查看「{self.get_property(v['property_id'])['address']}」"
            f"。如确认属实请回复/输入确认码 {v['confirm_code']}（规则版本 {rule['version']}）。",
            "viewing", viewing_id)
        return v

    def customer_confirm(self, viewing_id: str, code: str,
                         confirmed_at: str = "") -> dict:
        v = self.get_viewing(viewing_id)
        if v["status"] != "checked":
            raise DomainError("仅已签到的带看可做客户确认")
        if v["confirmed_at"]:
            return v
        if code.strip() != str(v["confirm_code"]):
            raise DomainError("客户确认码不正确")
        ts = confirmed_at or now_ts()
        with self._tx():
            self.conn.execute(
                "UPDATE viewings SET confirmed_at=? WHERE viewing_id=?", (ts, viewing_id))
            self._emit("viewing", viewing_id, "viewing.confirmed",
                       {"confirmed_at": ts}, actor="customer",
                       rule_version=v["rule_version"])
        return self.get_viewing(viewing_id)

    def get_viewing(self, vid: str) -> dict:
        r = self.row("SELECT * FROM viewings WHERE viewing_id=?", (vid,))
        if not r:
            raise DomainError("带看不存在")
        v = dict(r)
        v.update({
            "property": self.get_property(v["property_id"]),
            "customer": self.get_customer(v["customer_id"]),
            "agent": self.get_agent(v["agent_id"]),
        })
        s = self.row("SELECT name FROM stores WHERE store_id=?", (v["store_id"],))
        v["store_name"] = s["name"] if s else v["store_id"]
        v["protection"] = self.protection_window(v)
        return v

    def protection_window(self, v: dict) -> dict | None:
        """签到后才有保护期；返回起止与剩余天数。"""
        if v["status"] != "checked" or not v["checked_in_at"]:
            return None
        rule = self.get_rule(v["rule_version"])
        days = int(rule["body"].get("protection_days", 30))
        ci = v["checked_in_at"][:10]
        cidate = datetime.strptime(ci, "%Y-%m-%d").date()
        begin = cidate + timedelta(days=1)
        end = cidate + timedelta(days=days)
        today = now().date()
        return {
            "days": days,
            "begin": d(begin),
            "end": d(end),
            "today": d(today),
            "remaining": (end - today).days,
            "active": begin <= today <= end,
            "expired": today > end,
            "customer_confirmed": bool(v["confirmed_at"]),
        }

    def list_viewings(self, date_str: str = "", property_id: str = "",
                      customer_id: str = "") -> list[dict]:
        sql, args = "SELECT * FROM viewings WHERE 1=1", []
        if date_str:
            sql += " AND substr(appointment_time,1,10)=?"
            args.append(date_str)
        if property_id:
            sql += " AND property_id=?"
            args.append(property_id)
        if customer_id:
            sql += " AND customer_id=?"
            args.append(customer_id)
        sql += " ORDER BY appointment_time"
        return [self.get_viewing(r["viewing_id"])
                for r in self.rows(sql, tuple(args))]

    def calendar_month(self, year: int, month: int) -> dict:
        prefix = f"{year:04d}-{month:02d}"
        rows = self.rows(
            "SELECT * FROM viewings WHERE substr(appointment_time,1,7)=?"
            " ORDER BY appointment_time", (prefix,))
        days: dict[str, list] = {}
        for r in rows:
            v = self.get_viewing(r["viewing_id"])
            days.setdefault(r["appointment_time"][8:10], []).append(v)
        return {"month": prefix, "days": days}

    # ------------------------------------------------------------------ 成交
    def register_deal(self, property_id: str, customer_id: str,
                      amount_cents: int, deal_date: str, actor: str,
                      note: str = "") -> dict:
        prop = self.get_property(property_id)
        self.get_customer(customer_id)
        rule = self.active_rule()
        if amount_cents <= 0:
            raise DomainError("成交金额必须大于 0")

        did = new_id("deal")
        with self._tx():
            self.conn.execute(
                "INSERT INTO deals VALUES(?,?,?,?,?,?,?,?,?)",
                (did, property_id, customer_id, amount_cents, deal_date,
                 "", rule["version"], note, now_ts()))
            cands, in_window, _evidence = self._compute_candidates(
                prop, customer_id, deal_date, rule)
            for c in cands:
                self.conn.execute(
                    "INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?)",
                    (new_id("cand"), did, c.get("agent_id"), c.get("store_id"),
                     c["kind"], c["share_pct"], c["basis"], c["rank_no"],
                     json.dumps(c.get("viewing_ids", []))))
            agents = {c.get("agent_id") for c in cands if c.get("agent_id")}
            if not in_window:
                status = "保护期外"
            elif len(agents) >= 2:
                # 两名及以上人类经纪人候选才构成归属争议；
                # 单人 + 门店公盘（跨店分成/自行到店）按系统建议执行。
                status = "待裁决"
            else:
                status = "系统建议"
            self.conn.execute("UPDATE deals SET status=? WHERE deal_id=?",
                              (status, did))
            self._emit("deal", did, "deal.registered",
                       {"property_id": property_id, "customer_id": customer_id,
                        "amount_cents": amount_cents, "deal_date": deal_date,
                        "status": status,
                        "candidates": cands,
                        "in_protection_window": in_window},
                       actor=actor, rule_version=rule["version"])
        return self.get_deal(did)

    def _valid_evidence(self, property_id: str, customer_id: str) -> list[dict]:
        """已签到 + 客户已确认 + 未取消 的带看才算归属证据。"""
        rows = self.rows(
            "SELECT viewing_id FROM viewings WHERE property_id=? AND customer_id=?"
            " AND status='checked' AND confirmed_at IS NOT NULL"
            " ORDER BY appointment_time",
            (property_id, customer_id))
        return [self.get_viewing(r["viewing_id"]) for r in rows]

    def _compute_candidates(self, prop: dict, customer_id: str, deal_date: str,
                            rule: dict) -> tuple[list[dict], bool, list[dict]]:
        body = rule["body"]
        pdays = int(body.get("protection_days", 30))
        evidence = self._valid_evidence(prop["property_id"], customer_id)
        in_window = []
        for v in evidence:
            ci = v["checked_in_at"][:10]
            if is_protected(ci, deal_date, pdays):
                in_window.append(v)
        if not in_window:
            return [], False, evidence

        first, last = in_window[0], in_window[-1]
        first_share = float(body.get("first_view_share", 60))
        last_share = float(body.get("last_view_share", 40))
        raw = []  # (agent_id|WALKIN, store_id, view, share, basis)
        if first["viewing_id"] == last["viewing_id"]:
            raw.append((first["agent_id"], first["store_id"], first, 100.0,
                        "保护期内唯一有效带看"))
        else:
            raw.append((first["agent_id"], first["store_id"], first, first_share,
                        f"保护期内首带（{first['checked_in_at'][:10]}）"))
            raw.append((last["agent_id"], last["store_id"], last, last_share,
                        f"保护期内末带（{last['checked_in_at'][:10]}）"))

        # 合并同一经纪人（首末同人/多次带看）
        merged: dict[str, dict] = {}
        pool: dict[str, dict] = {}
        for agent_id, store_id, v, share, basis in raw:
            if agent_id == WALKIN:
                key = f"pool:{store_id}"
                slot = pool.setdefault(key, {"agent_id": None, "store_id": store_id,
                                             "kind": "store_pool", "share_pct": 0.0,
                                             "bases": [], "views": []})
                slot["share_pct"] += share
                slot["bases"].append(basis + "，客户自行到店")
                slot["views"].append(v["viewing_id"])
                continue
            slot = merged.setdefault(agent_id, {"agent_id": agent_id, "store_id": store_id,
                                                "kind": "agent", "share_pct": 0.0,
                                                "bases": [], "views": [], "collab": False})
            slot["share_pct"] += share
            slot["bases"].append(basis)
            slot["views"].append(v["viewing_id"])
            slot["collab"] = slot["collab"] or bool(v["collab"])

        # 跨店协作分佣：外店经纪人份额按规则与房源方门店公盘拆分
        cs = body.get("cross_store_split", {"bringing_agent": 50, "listing_store": 50})
        out: list[dict] = []
        for slot in merged.values():
            if slot["store_id"] != prop["store_id"] and slot["collab"]:
                a_pct = round(slot["share_pct"] * cs["bringing_agent"] / 100, 2)
                s_pct = round(slot["share_pct"] * cs["listing_store"] / 100, 2)
                lp = f"pool:{prop['store_id']}"
                pslot = pool.setdefault(
                    lp, {"agent_id": None, "store_id": prop["store_id"],
                         "kind": "store_pool", "share_pct": 0.0,
                         "bases": [], "views": []})
                pslot["share_pct"] += s_pct
                pslot["bases"].append(
                    f"跨店协作房源方分成（来自 {slot['agent_id']}）")
                pslot["views"].extend(slot["views"])
                slot["share_pct"] = a_pct
                slot["bases"].append("跨店协作，外店带看方分成")
            out.append(slot)
        out.extend(pool.values())

        total = sum(c["share_pct"] for c in out) or 1.0
        cands = []
        for i, c in enumerate(sorted(out, key=lambda x: -x["share_pct"]), start=1):
            share = round(c["share_pct"] / total * 100, 2)
            cands.append({
                "agent_id": c["agent_id"], "store_id": c["store_id"],
                "kind": c["kind"], "share_pct": share,
                "basis": "；".join(c["bases"]),
                "viewing_ids": c["views"], "rank_no": i,
            })
        return cands, True, evidence

    def get_deal(self, did: str) -> dict:
        r = self.row("SELECT * FROM deals WHERE deal_id=?", (did,))
        if not r:
            raise DomainError("成交不存在")
        deal = dict(r)
        deal["property"] = self.get_property(deal["property_id"])
        deal["customer"] = self.get_customer(deal["customer_id"])
        deal["rule"] = self.get_rule(deal["rule_version"])
        deal["candidates"] = [dict(x) for x in self.rows(
            "SELECT * FROM candidates WHERE deal_id=? ORDER BY rank_no", (did,))]
        for c in deal["candidates"]:
            c["viewing_ids"] = json.loads(c.get("viewing_ids") or "[]")
            if c["agent_id"]:
                c["agent"] = self.get_agent(c["agent_id"])
            s = self.row("SELECT name FROM stores WHERE store_id=?", (c["store_id"],))
            c["store_name"] = s["name"] if s else c["store_id"]
            c["amount_cents"] = int(deal["amount_cents"] * c["share_pct"] / 100)
        deal["evidence"] = self._evidence_bundle(did)
        rr = self.row("SELECT * FROM rulings WHERE deal_id=? ORDER BY decided_at DESC",
                      (did,))
        deal["ruling"] = self._ruling_dict(rr) if rr else None
        return deal

    def _evidence_bundle(self, did: str) -> list[dict]:
        """主管裁决页看到的证据：规则版本 + 每次带看签到/签字/客户确认。"""
        deal = self.row("SELECT * FROM deals WHERE deal_id=?", (did,))
        ev_views = self.rows(
            "SELECT * FROM viewings WHERE property_id=? AND customer_id=?"
            " ORDER BY appointment_time",
            (deal["property_id"], deal["customer_id"]))
        bundle = []
        for r in ev_views:
            v = dict(r)
            v["agent_name"] = (self.get_agent(v["agent_id"])
                               or {"name": v["agent_id"]})["name"]
            bundle.append(v)
        return bundle

    def _ruling_dict(self, r) -> dict:
        ruling = dict(r)
        ruling["split"] = json.loads(ruling["split_json"])
        ruling["evidence"] = json.loads(ruling["evidence_json"])
        decider = self.get_agent(ruling["decided_by"])
        ruling["decided_by_name"] = decider["name"] if decider else ruling["decided_by"]
        return ruling

    def list_deals(self, status: str = "") -> list[dict]:
        sql = "SELECT deal_id FROM deals"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY created_at DESC"
        return [self.get_deal(r["deal_id"]) for r in self.rows(sql, args)]

    # ------------------------------------------------------------------ 裁决
    def rule_deal(self, deal_id: str, decided_by: str, split: list[dict],
                  rationale: str) -> dict:
        """主管裁决。

        - 经纪人不能裁决自己的争议（decided_by 不得为候选经纪人）；
        - 必须是主管角色；
        - 裁决记录冻结规则版本与证据快照（签到时间、签字图、客户确认）。
        split: [{agent_id|store_id, share_pct}]，合计须 100。
        """
        deal = self.get_deal(deal_id)
        decider = self.get_agent(decided_by)
        candidate_agents = {c["agent_id"] for c in deal["candidates"] if c["agent_id"]}
        # 利益回避优先判定：哪怕该候选恰好有主管身份，也不得自裁
        if decided_by in candidate_agents:
            self._deny(decided_by, "deal.rule", "利益回避：候选经纪人不得裁决本案",
                       {"deal_id": deal_id})
            raise DomainError("利益回避：您是本案候选经纪人，不能裁决自己的归属争议")
        if not decider or decider["role"] != "supervisor":
            self._deny(decided_by, "deal.rule", "非主管尝试裁决",
                       {"deal_id": deal_id})
            raise DomainError("只有主管可以裁决归属争议")
        total = sum(float(s.get("share_pct", 0)) for s in split)
        if abs(total - 100) > 0.01:
            raise DomainError(f"分成比例合计须为 100%，当前 {total}%")
        if not rationale.strip():
            raise DomainError("裁决必须填写理由")

        evidence_snapshot = {
            "rule_version": deal["rule_version"],
            "rule_text": deal["rule"]["text"],
            "rule_body": deal["rule"]["body"],
            "deal": {"amount_cents": deal["amount_cents"],
                     "deal_date": deal["deal_date"]},
            "viewings": [{
                "viewing_id": v["viewing_id"],
                "agent_id": v["agent_id"], "agent_name": v["agent_name"],
                "store_id": v["store_id"],
                "appointment_time": v["appointment_time"],
                "status": v["status"],
                "checked_in_at": v["checked_in_at"],
                "sign_path": v["sign_path"],
                "confirmed_at": v["confirmed_at"],
                "collab": v["collab"],
                "cancel_reason": v["cancel_reason"],
            } for v in deal["evidence"]],
            "system_candidates": deal["candidates"],
        }
        rid = new_id("ruling")
        ts = now_ts()
        with self._tx():
            self.conn.execute(
                "INSERT INTO rulings VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rid, deal_id, decided_by, ts,
                 next((s.get("agent_id") for s in split if s.get("agent_id")), None),
                 next((s.get("store_id") for s in split if not s.get("agent_id")), None),
                 json.dumps(split, ensure_ascii=False), rationale,
                 deal["rule_version"], json.dumps(evidence_snapshot, ensure_ascii=False)))
            self.conn.execute("UPDATE deals SET status='已裁决' WHERE deal_id=?",
                              (deal_id,))
            self._emit("deal", deal_id, "deal.ruled",
                       {"ruling_id": rid, "decided_by": decided_by,
                        "split": split, "rationale": rationale},
                       actor=decided_by, rule_version=deal["rule_version"])
        return self.get_deal(deal_id)

    def list_rulings(self) -> list[dict]:
        return [self._ruling_dict(r) for r in self.rows(
            "SELECT * FROM rulings ORDER BY decided_at DESC")]

    # ------------------------------------------------------------- 审计/短信
    def audit_feed(self, limit: int = 100) -> list[dict]:
        rows = self.rows(
            "SELECT event_id,ts,stream_type,stream_id,type,actor,rule_version,data"
            " FROM events ORDER BY event_id DESC LIMIT ?", (limit,))
        return [dict(r) | {"data": json.loads(r["data"])} for r in rows]

    def denied_feed(self) -> list[dict]:
        return [dict(r) | {"detail": json.loads(r["detail"])}
                for r in self.rows(
                    "SELECT * FROM denied_actions ORDER BY ts DESC")]


class _ConnHolder:
    """让 Service 也能直接吃一个 sqlite 连接（主要用于测试）。"""
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def conn(self) -> sqlite3.Connection:
        return self._conn
