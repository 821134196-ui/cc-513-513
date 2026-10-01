"""种子数据：一条命令构造全部争议样例。

运行：python -m app.seed            # 仅当数据库不存在时播种
      python -m app.seed --force    # 删除旧库后重新播种
      ./run.sh                      # 播种（如需）并启动 Web

包含样例：
  1) 同一天多名经纪人带看 → 待裁决（首带 60 / 末带 40）
  2) 客户自行到店 + 经纪人补带 → 门店公盘候选
  3) 跨店协作：先拒绝未授权登记，主管授权后外店带看并参与分成
  4) 已取消带看不构成证据
  5) 保护期结束后成交 → 保护期外
  6) 已由主管裁决的历史案例（含经纪人自裁被拒留痕）
  7) 并发登记同一带看的竞态演示（恰好一条成功）
"""
from __future__ import annotations

import math
import os
import struct
import sys
import threading
import zlib

from . import db
from .domain import ConflictError, DomainError, Service
from .server import DB_PATH, SIGN_DIR
from .sms_adapter import SmsAdapter


# ------------------------------------------------------------- 极简 PNG 生成
def fake_signature_png(path: str, seed: int = 0) -> None:
    """无第三方依赖地生成一张带深色曲线的白底 PNG，模拟手写签字图。"""
    w, h = 520, 200
    buf = bytearray([255] * (w * h * 3))

    def px(x, y):
        x, y = int(x), int(y)
        if 0 <= x < w and 0 <= y < h:
            i = (y * w + x) * 3
            buf[i] = 0x26; buf[i + 1] = 0x26; buf[i + 2] = 0x2b

    def line(x0, y0, x1, y1):
        n = int(max(abs(x1 - x0), abs(y1 - y0), 1))
        for i in range(n + 1):
            px(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n)

    # 两条起伏笔画 + 收笔勾
    for stroke in range(2):
        pts = []
        for t in range(0, 301, 4):
            x = 60 + t + stroke * 90
            y = (110 + stroke * 22
                 + 26 * math.sin((t + seed * 30) / 22.0 + stroke * 1.7)
                 + 8 * math.sin((t + seed * 13) / 7.0))
            pts.append((x, y))
        for a, b in zip(pts, pts[1:]):
            line(a[0], a[1], b[0], b[1])
    line(360, 80 + seed % 20, 430, 140)

    raw = b"".join(b"\x00" + bytes(buf[r * w * 3:(r + 1) * w * 3])
                   for r in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def sign(svc: Service, vid: str, checkin_ts: str, confirm_ts: str) -> None:
    """签到（附模拟签字图）并以短信码完成客户确认。"""
    fname = f"{vid}_seed.png"
    fpath = os.path.join(SIGN_DIR, fname)
    fake_signature_png(fpath, seed=abs(hash(vid)) % 50)
    rel = f"/signatures/{fname}"
    actor = svc.get_viewing(vid)["agent_id"]
    svc.check_in(vid, 39.9900, 116.4700, rel, actor=actor, checked_at=checkin_ts)
    code = svc.get_viewing(vid)["confirm_code"]
    svc.customer_confirm(vid, code, confirmed_at=confirm_ts)


RULE_TEXT = """《望京·国贸团队佣金归属规则（签署版 v2026.09）》

第一条 保护期
1.1 客户就某一房源完成带看签到并经客户本人确认后，自次日起 30 个自然日内为保护期；
    保护期最后一天成交仍受保护，次日起为保护期外。
1.2 未签到、未经客户短信确认或已取消的带看，不产生任何保护与归属主张。

第二条 归属候选
2.1 保护期内成交，首带经纪人候选 60%，末带经纪人候选 40%；首末为同一人时 100%。
2.2 客户自行到门店登记（非经纪人带看）的，对应份额计入接待门店公盘，不计给个人。
2.3 同一天多名经纪人先后带看的，系统同时列出全部候选及其证据，由房源方门店主管裁决；
    经纪人不得裁决本人作为候选的争议。

第三条 跨门店协作
3.1 外店经纪人带看他店房源，须事先取得房源方门店主管的协作授权，否则系统拒绝登记并留痕。
3.2 已授权的跨店带看，外店带看方就其候选份额分得 50%，其余 50% 归房源方门店公盘。

第四条 裁决
4.1 主管裁决时，系统必须同时呈现：本规则版本全文、每次带看的签到时间、客户签字图、
    客户确认时间。裁决结果与证据快照永久留痕，规则版本按成交时冻结版本执行。
"""


def seed(force: bool = False) -> None:
    os.makedirs(SIGN_DIR, exist_ok=True)
    if force and os.path.exists(DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            p = DB_PATH + suffix
            if os.path.exists(p):
                os.remove(p)
        # 清理上一轮生成的签字图（纯生成产物）
        for old in os.listdir(SIGN_DIR):
            if old.endswith(".png"):
                os.remove(os.path.join(SIGN_DIR, old))
    if os.path.exists(DB_PATH) and not force:
        print(f"数据库已存在：{DB_PATH}（如需重建：python -m app.seed --force）")
        return

    database = db.Database(DB_PATH)
    svc = Service(database, SmsAdapter(database.conn(), echo=False))

    # ----- 组织与规则
    svc.add_store("S1", "望京旗舰店")
    svc.add_store("S2", "国贸分店")
    svc.add_agent("A001", "王敏", "S1", "agent", "13911110001")
    svc.add_agent("A002", "李航", "S1", "agent", "13911110002")
    svc.add_agent("A003", "赵倩", "S2", "agent", "13911110003")
    svc.add_agent("SUP1", "周正", "S1", "supervisor", "13911110009")
    svc.add_agent("SUP2", "钱娜", "S2", "supervisor", "13911110008")

    svc.publish_rule(
        "v2026.09",
        {"protection_days": 30, "first_view_share": 60, "last_view_share": 40,
         "cross_store_split": {"bringing_agent": 50, "listing_store": 50}},
        RULE_TEXT, published_by="SUP1")

    # ----- 房源
    p1 = svc.add_property("P1", "望京西园三区 8 号楼 1203 室", "S1",
                          368_000_000, "南北两居", 89.4, "2026-08-01")["property_id"]
    p2 = svc.add_property("P2", "阜通东大街 6 号院 2 号楼 502 室", "S1",
                          295_000_000, "东南一居", 56.1, "2026-08-20")["property_id"]
    p3 = svc.add_property("P3", "国贸中环公寓 A 座 1801 室", "S2",
                          520_000_000, "高层两居", 96.0, "2026-09-01")["property_id"]

    # ----- 客户（含去重演示）
    c1 = svc.register_customer("陈立", "13800000001", "A001", "改善购房")["customer_id"]
    try:  # 精确重复：同手机号再次登记 → 拒绝并留痕
        svc.register_customer("陈先生", "13800000001", "A002")
    except DomainError:
        pass
    try:  # 疑似重复：同名 + 手机后四位相同 → 需人工确认
        svc.register_customer("陈立", "13600000001", "A002")
    except DomainError:
        svc.register_customer("陈立", "13600000001", "A002", force=True)  # 确为另一人

    c2 = svc.register_customer("林芳", "13800000002", "A001")["customer_id"]
    c3 = svc.register_customer("吴桐", "13800000003", "A003")["customer_id"]
    c4 = svc.register_customer("高源", "13800000004", "A002")["customer_id"]
    c5 = svc.register_customer("苏禾", "13800000005", "A001")["customer_id"]
    c6 = svc.register_customer("何悦", "13800000006", "A002")["customer_id"]

    # ===== 样例 1：同一天多名经纪人带看 → 待裁决
    v1a = svc.schedule_viewing(p1, c1, "A001", "2026-09-12 10:00")["viewing_id"]
    v1b = svc.schedule_viewing(p1, c1, "A002", "2026-09-12 14:00")["viewing_id"]
    sign(svc, v1a, "2026-09-12 10:34:00", "2026-09-12 10:50:00")
    sign(svc, v1b, "2026-09-12 14:26:00", "2026-09-12 15:02:00")
    d1 = svc.register_deal(p1, c1, 368_000_000, "2026-09-28", "SUP1",
                           "同日两经纪人带看，待主管裁决")
    assert d1["status"] == "待裁决", d1["status"]

    # ===== 样例 2：客户自行到店（门店公盘）+ 经纪人末带
    v2a = svc.schedule_viewing(p2, c2, "WALKIN", "2026-09-15 11:00",
                               store_id_override="S1")["viewing_id"]
    v2b = svc.schedule_viewing(p2, c2, "A001", "2026-09-20 15:00")["viewing_id"]
    sign(svc, v2a, "2026-09-15 11:09:00", "2026-09-15 11:20:00")
    sign(svc, v2b, "2026-09-20 15:31:00", "2026-09-20 16:00:00")
    d2 = svc.register_deal(p2, c2, 295_000_000, "2026-09-29", "SUP1")
    assert d2["status"] == "系统建议", d2["status"]

    # ===== 样例 3：跨店协作（先拒后授）
    try:  # 未授权：S1 经纪人登记 S2 房源 → 拒绝留痕
        svc.schedule_viewing(p3, c3, "A001", "2026-09-17 10:00")
        raise AssertionError("应当拒绝未授权跨店登记")
    except DomainError:
        pass
    svc.grant_collab(p3, "A001", "SUP2")  # 房源方门店主管授权
    v3a = svc.schedule_viewing(p3, c3, "A001", "2026-09-18 10:00")["viewing_id"]
    v3b = svc.schedule_viewing(p3, c3, "A003", "2026-09-25 16:00")["viewing_id"]
    sign(svc, v3a, "2026-09-18 10:41:00", "2026-09-18 11:00:00")
    sign(svc, v3b, "2026-09-25 16:33:00", "2026-09-25 17:10:00")
    d3 = svc.register_deal(p3, c3, 520_000_000, "2026-09-30", "SUP2",
                           "跨店协作 + 本店末带，待裁决")
    assert d3["status"] == "待裁决", d3["status"]

    # ===== 样例 4：取消带看不算证据
    v4x = svc.schedule_viewing(p2, c4, "A002", "2026-09-10 09:30")["viewing_id"]
    svc.cancel_viewing(v4x, "A002", "客户临时加班改期", "2026-09-10 08:50:00")
    v4a = svc.schedule_viewing(p2, c4, "A002", "2026-09-16 10:00")["viewing_id"]
    sign(svc, v4a, "2026-09-16 10:35:00", "2026-09-16 11:00:00")
    d4 = svc.register_deal(p2, c4, 290_000_000, "2026-09-29", "SUP1")
    assert d4["status"] == "系统建议" and d4["candidates"][0]["agent_id"] == "A002"

    # ===== 样例 5：保护期结束后的成交
    v5a = svc.schedule_viewing(p1, c5, "A001", "2026-08-20 13:00")["viewing_id"]
    sign(svc, v5a, "2026-08-20 13:40:00", "2026-08-20 14:00:00")
    d5 = svc.register_deal(p1, c5, 365_000_000, "2026-10-01", "SUP1",
                           "距唯一带看已超过 30 天保护期")
    assert d5["status"] == "保护期外", d5["status"]

    # ===== 样例 6：已裁决历史案（含利益回避演示）
    c6_first = svc.schedule_viewing(p2, c6, "A001", "2026-09-05 10:00")["viewing_id"]
    c6_last = svc.schedule_viewing(p2, c6, "A002", "2026-09-08 10:00")["viewing_id"]
    sign(svc, c6_first, "2026-09-05 10:30:00", "2026-09-05 10:45:00")
    sign(svc, c6_last, "2026-09-08 10:31:00", "2026-09-08 11:00:00")
    d6 = svc.register_deal(p2, c6, 295_000_000, "2026-09-20", "SUP1")
    # A001 试图裁决自己的争议 → 被拒留痕
    try:
        svc.rule_deal(d6["deal_id"], "A001",
                      [{"agent_id": "A001", "share_pct": 100.0}], "我先带的")
        raise AssertionError("经纪人不应能裁决自己的争议")
    except DomainError:
        pass
    svc.rule_deal(
        d6["deal_id"], "SUP1",
        [{"agent_id": "A001", "share_pct": 50.0},
         {"agent_id": "A002", "share_pct": 50.0}],
        "两次带看均有签到与客户短信确认，相差 3 天且客户多次咨询，"
        "按规则首末双方贡献相当，裁量各 50%。规则版本 v2026.09。")

    # ----- 未来日程（供日历/签到 UI 演示）
    svc.schedule_viewing(p1, c1, "A001", "2026-10-08 10:00")
    svc.schedule_viewing(p3, c3, "A003", "2026-10-03 15:00")
    svc.schedule_viewing(p2, c2, "A001", "2026-10-12 14:30")

    # ===== 样例 7：并发登记同一带看，只能成功一条
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def concurrent(agent: str):
        try:
            barrier.wait()
            svc.schedule_viewing(p1, c5, agent, "2026-10-15 09:00",
                                 ide_key=f"race-{agent}")
            results[agent] = "ok"
        except ConflictError:
            results[agent] = "conflict"
        except DomainError as e:
            results[agent] = f"denied:{e}"

    t1 = threading.Thread(target=concurrent, args=("A001",))
    t2 = threading.Thread(target=concurrent, args=("A002",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results.values()).count("ok") == 1, results
    assert "conflict" in results.values(), results

    _c = database.conn()
    n_deals = _c.execute("SELECT COUNT(*) c FROM deals").fetchone()["c"]
    n_events = _c.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    n_denied = _c.execute("SELECT COUNT(*) c FROM denied_actions").fetchone()["c"]
    print("──────────────────────────────────────────")
    print("种子数据已就绪：")
    print(f"  成交案例 {n_deals} 条（待裁决 ×2 / 系统建议 ×2 / 保护期外 ×1 / 已裁决 ×1）")
    print(f"  事件 {n_events} 条；拒绝动作留痕 {n_denied} 条；并发竞争结果 {results}")
    print(f"  数据库 {os.path.abspath(DB_PATH)}")
    print("──────────────────────────────────────────")


if __name__ == "__main__":
    seed(force="--force" in sys.argv)
