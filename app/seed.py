"""种子数据：一条命令生成完整演示库，含多经纪人同日冲突、跨店协作、
取消、自行到店、保护期外成交、已裁决/待裁决争议等样例。"""
import argparse
import base64
import hashlib
import json
import os
import zlib
from datetime import date

from . import db as store
from . import domain as D

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "app.db")

TODAY = "2026-10-01"

RULE_V1 = {
    "window_days": 30,
    "sole_first": {"viewer_share": 0.80},
    "same_day": {"home_store_share_when_cross": 0.20},
    "cross_store": {"home_store_share": 0.30},
    "no_protection": {"home_store_share": 1.0},
}
RULE_V2 = {
    "window_days": 60,
    "sole_first": {"viewer_share": 0.70},
    "same_day": {"home_store_share_when_cross": 0.25},
    "cross_store": {"home_store_share": 0.35},
    "no_protection": {"home_store_share": 1.0},
}


def _png_data_url(seed_text, w=480, h=120):
    """生成一张带几笔签名线条的 PNG（纯标准库，确定性）。"""
    seed = int(hashlib.sha256(seed_text.encode()).hexdigest()[:12], 16)
    rnd = seed
    def rr():
        nonlocal rnd
        rnd = (rnd * 1103515245 + 12345) & 0x7fffffff
        return rnd / 0x7fffffff

    rows = [bytearray(b"\xff" * (w * 3)) for _ in range(h)]
    nstrokes = 3 + int(rr() * 2)
    for _ in range(nstrokes):
        x, y = 20 + rr() * 60, 30 + rr() * (h - 60)
        for _i in range(40):
            nx = x + 8 + rr() * 6
            ny = y + (rr() - 0.5) * 22
            for gx in range(int(x), min(int(nx), w)):
                gy = int(y + (ny - y) * (gx - x) / max(nx - x, 1))
                for dy in (0, 1):
                    if 0 <= gy + dy < h:
                        off = gx * 3
                        rows[gy + dy][off:off + 3] = b"\x33\x33\x33"
            x, y = nx, ny
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    comp = zlib.compress(raw, 9)

    def chunk(tag, data):
        c = tag + data
        return (len(data).to_bytes(4, "big") + c
                + zlib.crc32(c).to_bytes(4, "big"))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big")
                   + bytes([8, 2, 0, 0, 0]))
           + chunk(b"IDAT", comp) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


def seed(db_path=DB_PATH, reset=False):
    if reset and os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
    store.init_db(db_path)
    conn = store.get_conn(db_path)
    try:
        if conn.execute("SELECT COUNT(*) c FROM stores").fetchone()["c"]:
            if not reset:
                print("数据库已有数据，跳过种子。使用 --reset 重建。")
                return
            raise SystemExit("数据库非空且未重置，中止")

        ts = store.now_iso()
        conn.execute("INSERT INTO stores(id,code,name,created_ts) VALUES (1,'ZS','中山广场店',?)", (ts,))
        conn.execute("INSERT INTO stores(id,code,name,created_ts) VALUES (2,'DG','东港店',?)", (ts,))

        agents = [
            (1, 1, "A001", "王梅", "13911110001", "agent", 1),
            (2, 1, "A002", "李强", "13911110002", "agent", 0),
            (3, 1, "A003", "周敏", "13911110003", "agent", 1),
            (4, 2, "A004", "赵航", "13911110004", "agent", 1),
            (5, 1, "M001", "陈岚（中山广场店主管）", "13911110099", "supervisor", 1),
            (6, 2, "M002", "孙岩（东港店主管）", "13911110098", "supervisor", 1),
        ]
        conn.executemany(
            """INSERT INTO agents(id,store_id,employee_no,name,phone,role,can_cross_store,active)
               VALUES (?,?,?,?,?,?,?,1)""", agents)

        conn.execute(
            """INSERT INTO rule_versions(id,version,title,effective_from,body_json,created_by,published_ts)
               VALUES (1,'2025-v1','团队佣金与首看保护规则（初版）','2025-01-01',?,5,?)""",
            (json.dumps(RULE_V1, ensure_ascii=False), ts))
        conn.execute(
            """INSERT INTO rule_versions(id,version,title,effective_from,body_json,created_by,published_ts)
               VALUES (2,'2026-v2','团队佣金与首看保护规则（保护期延长至60日）','2026-09-01',?,5,?)""",
            (json.dumps(RULE_V2, ensure_ascii=False), ts))

        props = [
            (1, 1, "中山区长江路88号2单元401室", "中山区", 3200000, "on"),
            (2, 1, "沙河口区星海街12号1单元1802室", "沙河口区", 2650000, "dealt"),
            (3, 1, "西岗区胜利路5号3单元302室", "西岗区", 1880000, "dealt"),
            (4, 2, "东港商务区港隆路9号6-1501", "中山区", 4100000, "dealt"),
            (5, 1, "中山区人民路9号9-1102", "中山区", 2360000, "on"),
            (6, 1, "沙河口区五一路21号4-701", "沙河口区", 1520000, "on"),
            (7, 1, "中山区枫林街30号2-202", "中山区", 2080000, "on"),
        ]
        conn.executemany(
            "INSERT INTO properties(id,store_id,address,district,price,status,listed_ts) "
            "VALUES (?,?,?,?,?,?,'2026-05-01T00:00:00Z')", props)

        def client(name, phone, idno=None, store_id=1):
            c, created = D.get_or_create_client(conn, name, phone, idno, store_id, actor="seed")
            return c["id"]

        c1 = client("刘洋", "13800000001", "210202199003071234")
        c2 = client("张静", "13800000002")
        c3 = client("吴磊", "13800000003")
        c4 = client("郑爽", "13800000004")
        c5 = client("冯雪", "13800000005")
        c7 = client("何军", "13800000007")
        # 同一手机号、不同留资名：必须去重为同一客户
        c6a, created6 = D.get_or_create_client(conn, "许晴", "13900000006", None, 1, "seed")
        c6b, _ = D.get_or_create_client(conn, "许晴（展会留资）", "13900000006", None, 1, "seed")
        assert c6a["id"] == c6b["id"], "客户去重失败"
        conn.execute("INSERT INTO client_aliases(client_id,kind,value,created_ts) VALUES (?, 'name', '小许', ?)",
                     (c6a["id"], ts))
        c6 = c6a["id"]

        def schedule(pid, cid, when, agent=None, self_visit=False, cross=False, note=""):
            vid, warn = D.register_viewing(
                conn, cid, pid, when, agent_id=agent, is_self_visit=self_visit,
                cross_store_requested=cross, note=note, actor="seed",
                idem_key=f"{pid}-{cid}-{when}-{agent}-{self_visit}")
            return vid, warn

        def checkin(vid, tag, confirmed=True):
            D.check_in(conn, vid,
                       sig_agent_data=_png_data_url(f"agent-{tag}"),
                       sig_client_data=_png_data_url(f"client-{tag}"),
                       client_confirmed=confirmed, actor="seed")

        # —— P1：同一天三名经纪人带看（含跨店），保护期内成交，争议待裁决 ——
        v1, _ = schedule(1, c1, "2026-09-20T10:00", agent=1, note="首看登记 王梅")
        v3, _ = schedule(1, c1, "2026-09-20T11:00", agent=3, note="同日 周敏")
        v4, _ = schedule(1, c1, "2026-09-20T14:00", agent=4, cross=True, note="同日跨店 赵航")
        checkin(v1, "p1-a1", True)
        checkin(v3, "p1-a3", True)
        checkin(v4, "p1-a4", False)   # 跨店经纪人证据缺客户确认，裁决前必须补齐
        deal1, _ = D.record_deal(conn, 1, c1, "2026-09-28", 3180000, 31800, actor="seed")
        d1 = conn.execute("SELECT id FROM disputes WHERE deal_id=? AND status='open'", (deal1,)).fetchone()
        print(f"[种子] 争议#1 多经纪人同日+跨店（deal={deal1}）：门店25% + 三人各25% 建议，待主管裁决")

        # —— P2：独家首看人，保护期内成交，无争议 ——
        v, _ = schedule(2, c2, "2026-09-15T09:30", agent=1)
        checkin(v, "p2-a1", True)
        D.record_deal(conn, 2, c2, "2026-09-25", 2600000, 26000, actor="seed")

        # —— P3：保护期结束后成交，佣金归房源方门店 ——
        v, _ = schedule(3, c3, "2026-06-10T13:00", agent=2)
        checkin(v, "p3-a2", True)
        D.record_deal(conn, 3, c3, "2026-09-30", 1880000, 18800, actor="seed")

        # —— P4：跨店独家协作带看，争议已由非利益相关主管裁决（留痕） ——
        v, _ = schedule(4, c4, "2026-09-18T15:00", agent=1, cross=True)
        checkin(v, "p4-a1", True)
        deal4, _ = D.record_deal(conn, 4, c4, "2026-09-29", 4100000, 41000, actor="seed")
        d4id = conn.execute("SELECT id FROM disputes WHERE deal_id=?", (deal4,)).fetchone()["id"]
        d4 = D.get_dispute(conn, d4id)
        outcome = {}
        for cand in d4["candidates"]:
            outcome[cand["id"]] = cand["suggested_share"]
        D.decide_dispute(conn, d4id, decided_by=6, outcome=outcome,
                         note="核对 2026-v2 规则、签到与客户确认无误：东港店作为房源方留存35%，"
                              "王梅跨店协作分得65%。", evidence_reviewed=True, actor=6)
        print(f"[种子] 争议#2 跨店协作（deal={deal4}）已裁决并留痕")

        # —— P5：取消 + 客户自行到店 + 未来预约 ——
        vc, _ = schedule(5, c5, "2026-09-28T10:00", agent=3, note="客户临时改期")
        D.cancel_viewing(conn, vc, reason="客户提前两小时改期，未发生带看", actor="seed")
        vs, _ = schedule(5, c5, "2026-09-29T11:00", self_visit=True, note="客户自行到店看房")
        D.check_in(conn, vs, sig_client_data=_png_data_url("self-p5"),
                   client_confirmed=True, actor="seed")
        schedule(5, c5, "2026-10-02T10:00", agent=2, note="改期后重新预约")

        # —— P6：今日待签到（签到页演示） ——
        schedule(6, c6, "2026-10-01T14:00", agent=1, note="线上留资客户，今日带看")

        # —— P7：后到经纪人登记时，原首看人仍在保护期 -> 模拟短信提醒 ——
        v, _ = schedule(7, c7, "2026-09-25T10:00", agent=1, note="首看 王梅")
        checkin(v, "p7-a1", True)
        _, warn = schedule(7, c7, "2026-10-03T09:30", agent=2, note="李强二次带看，触发保护期提示")
        assert warn and warn["agent_id"] == 1

        store.append_event(conn, "seed_completed", {"date": TODAY}, actor="seed")
        n = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
        print(f"[种子] 完成：7 套房源、7 位客户、{n} 条事件；库文件 {db_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()
    seed(args.db, args.reset)
