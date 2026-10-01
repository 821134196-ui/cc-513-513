"""领域规则测试：保护期边界 / 客户去重 / 跨店权限 / 裁决留痕 / 并发双记录。

运行：python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.domain import ConflictError, DomainError, Service, WALKIN  # noqa: E402
from app.sms_adapter import SmsAdapter  # noqa: E402
from app.timeutil import is_protected  # noqa: E402

RULE_TEXT = "测试规则 v1：保护期 30 天，首带 60 末带 40，跨店 50/50。"
BODY = {"protection_days": 30, "first_view_share": 60, "last_view_share": 40,
        "cross_store_split": {"bringing_agent": 50, "listing_store": 50}}


class CaseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "test.db")
        self.database = db.Database(self.path)
        self.svc = Service(self.database,
                           SmsAdapter(self.database.conn(), echo=False))
        self._bootstrap()

    def tearDown(self):
        # 关闭线程局部连接
        import sqlite3
        try:
            self.database.conn().close()
        except sqlite3.Error:
            pass
        self.tmp.cleanup()

    def _bootstrap(self):
        s = self.svc
        s.add_store("S1", "望京店"); s.add_store("S2", "国贸店")
        s.add_agent("A1", "王敏", "S1", "agent")
        s.add_agent("A2", "李航", "S1", "agent")
        s.add_agent("A3", "赵倩", "S2", "agent")
        s.add_agent("B1", "周正", "S1", "supervisor")
        s.add_agent("B2", "钱娜", "S2", "supervisor")
        s.publish_rule("v1", BODY, RULE_TEXT, "B1")
        self.p1 = s.add_property("P1", "望京西园 1 号 101", "S1", 3_000_000_00)[
            "property_id"]
        self.p2 = s.add_property("P2", "国贸公寓 2 号 202", "S2", 5_000_000_00)[
            "property_id"]

    def customer(self, name="测试客户", phone="13800000001", agent="A1"):
        return self.svc.register_customer(name, phone, agent)["customer_id"]

    def proven_viewing(self, pid, cid, agent, appt, checkin=None, confirm=None):
        """登记→签到（含签字）→客户确认，返回带看 dict。"""
        vid = self.svc.schedule_viewing(pid, cid, agent, appt)["viewing_id"]
        self.svc.check_in(vid, 0, 0, f"/signatures/{vid}.png",
                          actor=agent, checked_at=checkin or appt + ":30")
        self.svc.customer_confirm(
            vid, self.svc.get_viewing(vid)["confirm_code"],
            confirmed_at=confirm or appt + ":59")
        return self.svc.get_viewing(vid)


# --------------------------------------------------------------- 保护期边界
class TestProtectionBoundary(CaseBase):
    def test_calendar_function_boundaries(self):
        # 带看签到日 D = 2026-09-01，保护期窗口 [D+1, D+30]
        self.assertFalse(is_protected("2026-09-01", "2026-09-01", 30))  # D 当天不算
        self.assertTrue(is_protected("2026-09-01", "2026-09-02", 30))   # D+1 起算
        self.assertTrue(is_protected("2026-09-01", "2026-10-01", 30))   # D+30 仍受保护
        self.assertFalse(is_protected("2026-09-01", "2026-10-02", 30))  # D+31 失效

    def test_deal_on_last_protected_day_is_covered(self):
        cid = self.customer()
        self.proven_viewing(self.p1, cid, "A1", "2026-09-01 10:00")
        deal = self.svc.register_deal(self.p1, cid, 3_000_000_00,
                                      "2026-10-01", "B1")
        self.assertEqual(deal["status"], "系统建议")
        self.assertEqual(deal["candidates"][0]["agent_id"], "A1")

    def test_deal_day_after_window_is_outside(self):
        cid = self.customer()
        self.proven_viewing(self.p1, cid, "A1", "2026-09-01 10:00")
        deal = self.svc.register_deal(self.p1, cid, 3_000_000_00,
                                      "2026-10-02", "B1")
        self.assertEqual(deal["status"], "保护期外")
        self.assertEqual(deal["candidates"], [])

    def test_unconfirmed_or_cancelled_viewing_grants_no_protection(self):
        cid = self.customer()
        # 已签到但客户未确认
        vid = self.svc.schedule_viewing(
            self.p1, cid, "A1", "2026-09-10 10:00")["viewing_id"]
        self.svc.check_in(vid, 0, 0, f"/signatures/{vid}.png", actor="A1")
        deal = self.svc.register_deal(self.p1, cid, 3_000_000_00,
                                      "2026-09-20", "B1")
        self.assertEqual(deal["status"], "保护期外")

        # 已取消带看也不算
        cid2 = self.customer(phone="13800000002")
        v2 = self.svc.schedule_viewing(
            self.p1, cid2, "A1", "2026-09-10 11:00")["viewing_id"]
        self.svc.cancel_viewing(v2, "A1", "客户改期")
        deal2 = self.svc.register_deal(self.p1, cid2, 3_000_000_00,
                                       "2026-09-20", "B1")
        self.assertEqual(deal2["status"], "保护期外")


# ------------------------------------------------------------------ 客户去重
class TestCustomerDedup(CaseBase):
    def test_exact_duplicate_phone_rejected_and_logged(self):
        self.customer(phone="13812340000")
        with self.assertRaises(DomainError):
            self.svc.register_customer("另一个名字", "13812340000", "A2")
        denied = self.svc.denied_feed()
        self.assertTrue(any(d["action"] == "customer.register" for d in denied))

    def test_suspected_duplicate_requires_confirmation(self):
        self.customer(name="陈立", phone="13812340001")
        # 同名 + 后四位相同、号码不同 → 疑似，先拒绝
        with self.assertRaises(DomainError):
            self.svc.register_customer("陈立", "13612340001", "A2")
        # force 后允许（确为另一人）
        c = self.svc.register_customer("陈立", "13612340001", "A2", force=True)
        self.assertTrue(c["customer_id"])

    def test_different_person_not_flagged(self):
        self.customer(name="张三", phone="13800001111")
        dupes = self.svc.find_duplicates("李四", "13900002222")
        self.assertEqual(dupes, [])


# ------------------------------------------------------------------ 跨店权限
class TestCrossStore(CaseBase):
    def test_unauthorized_cross_store_schedule_denied(self):
        cid = self.customer(phone="13800000010")
        # A1 属于 S1，P2 是 S2 房源
        with self.assertRaises(DomainError):
            self.svc.schedule_viewing(self.p2, cid, "A1", "2026-09-10 10:00")
        rows = self.svc.list_viewings(property_id=self.p2)
        self.assertEqual(rows, [])  # 未产生任何记录
        denied = self.svc.denied_feed()
        self.assertTrue(any("跨店" in d["reason"] for d in denied))

    def test_grant_requires_listing_store_supervisor(self):
        cid = self.customer(phone="13800000011")
        # 非主管不能授权
        with self.assertRaises(DomainError):
            self.svc.grant_collab(self.p2, "A1", "A3")
        # 非房源方主管不能授权（B1 是 S1 主管）
        with self.assertRaises(DomainError):
            self.svc.grant_collab(self.p2, "A1", "B1")
        # 房源方 S2 主管 B2 授权成功
        self.svc.grant_collab(self.p2, "A1", "B2")
        v = self.svc.schedule_viewing(self.p2, cid, "A1", "2026-09-10 10:00")
        self.assertEqual(v["collab"], 1)

    def test_cross_store_split_creates_listing_store_pool_share(self):
        cid = self.customer(phone="13800000012")
        self.svc.grant_collab(self.p2, "A1", "B2")
        # 外店 A1 首带，本店 A3 末带
        self.proven_viewing(self.p2, cid, "A1", "2026-09-10 10:00")
        self.proven_viewing(self.p2, cid, "A3", "2026-09-15 10:00")
        deal = self.svc.register_deal(self.p2, cid, 5_000_000_00,
                                      "2026-09-20", "B2")
        self.assertEqual(deal["status"], "待裁决")
        by_agent = {c["agent_id"]: c for c in deal["candidates"]}
        # A1 的首带 60% 再按 50/50 拆：A1 30%，S2 公盘 30%
        self.assertAlmostEqual(by_agent["A1"]["share_pct"], 30.0, places=2)
        pool = next(c for c in deal["candidates"] if c["kind"] == "store_pool")
        self.assertEqual(pool["store_id"], "S2")
        self.assertAlmostEqual(pool["share_pct"], 30.0, places=2)
        # A3 末带 40% 为同店，全额
        self.assertAlmostEqual(by_agent["A3"]["share_pct"], 40.0, places=2)
        total = sum(c["share_pct"] for c in deal["candidates"])
        self.assertAlmostEqual(total, 100.0, places=2)


# ------------------------------------------------------------------ 到店/同天
class TestWalkinAndSameDay(CaseBase):
    def test_walkin_share_goes_to_store_pool_not_person(self):
        cid = self.customer(phone="13800000020")
        self.proven_viewing(self.p1, cid, WALKIN, "2026-09-10 11:00",
                            checkin="2026-09-10 11:09", confirm="2026-09-10 11:20")
        self.proven_viewing(self.p1, cid, "A1", "2026-09-12 10:00")
        deal = self.svc.register_deal(self.p1, cid, 3_000_000_00,
                                      "2026-09-20", "B1")
        # 只有一个人类经纪人候选 → 系统建议（公盘不构成经纪人争议）
        self.assertEqual(deal["status"], "系统建议")
        pool = next(c for c in deal["candidates"] if c["kind"] == "store_pool")
        self.assertEqual(pool["store_id"], "S1")
        self.assertGreater(pool["share_pct"], 0)
        person = next(c for c in deal["candidates"] if c["kind"] == "agent")
        self.assertEqual(person["agent_id"], "A1")

    def test_same_day_two_agents_becomes_dispute_with_both_candidates(self):
        cid = self.customer(phone="13800000021")
        self.proven_viewing(self.p1, cid, "A1", "2026-09-12 10:00")
        self.proven_viewing(self.p1, cid, "A2", "2026-09-12 14:00")
        deal = self.svc.register_deal(self.p1, cid, 3_000_000_00,
                                      "2026-09-20", "B1")
        self.assertEqual(deal["status"], "待裁决")
        agents = {c["agent_id"] for c in deal["candidates"]}
        self.assertEqual(agents, {"A1", "A2"})
        shares = sorted(c["share_pct"] for c in deal["candidates"])
        self.assertEqual(shares, [40.0, 60.0])


# ------------------------------------------------------------------ 裁决留痕
class TestRuling(CaseBase):
    def _dispute(self):
        cid = self.customer(phone="13800000030")
        self.proven_viewing(self.p1, cid, "A1", "2026-09-12 10:00")
        self.proven_viewing(self.p1, cid, "A2", "2026-09-12 14:00")
        return self.svc.register_deal(self.p1, cid, 3_000_000_00,
                                      "2026-09-20", "B1")

    def test_agent_cannot_rule_own_dispute(self):
        deal = self._dispute()
        with self.assertRaises(DomainError):
            self.svc.rule_deal(deal["deal_id"], "A1",
                               [{"agent_id": "A1", "share_pct": 100.0}], "我先带")
        denied = self.svc.denied_feed()
        self.assertTrue(any("利益回避" in d["reason"] for d in denied))

    def test_non_supervisor_cannot_rule(self):
        deal = self._dispute()
        # A3 是经纪人且非候选，也不能裁决
        with self.assertRaises(DomainError):
            self.svc.rule_deal(deal["deal_id"], "A3",
                               [{"agent_id": "A1", "share_pct": 100.0}], "理由")

    def test_split_must_total_100_and_require_rationale(self):
        deal = self._dispute()
        with self.assertRaises(DomainError):
            self.svc.rule_deal(deal["deal_id"], "B1",
                               [{"agent_id": "A1", "share_pct": 70.0},
                                {"agent_id": "A2", "share_pct": 20.0}], "合计90")
        with self.assertRaises(DomainError):
            self.svc.rule_deal(deal["deal_id"], "B1",
                               [{"agent_id": "A1", "share_pct": 50.0},
                                {"agent_id": "A2", "share_pct": 50.0}], "  ")

    def test_supervisor_ruling_freezes_evidence_and_rule_version(self):
        deal = self._dispute()
        ruled = self.svc.rule_deal(
            deal["deal_id"], "B1",
            [{"agent_id": "A1", "share_pct": 50.0},
             {"agent_id": "A2", "share_pct": 50.0}],
            "两次带看均签到且客户确认，贡献相当，各半。")
        self.assertEqual(ruled["status"], "已裁决")
        r = ruled["ruling"]
        self.assertEqual(r["decided_by"], "B1")
        self.assertEqual(r["rule_version"], "v1")
        snap = r["evidence"]
        self.assertEqual(snap["rule_version"], "v1")
        self.assertIn("保护期 30 天", snap["rule_text"])
        self.assertEqual(len(snap["viewings"]), 2)
        for vv in snap["viewings"]:
            self.assertTrue(vv["checked_in_at"])
            self.assertTrue(vv["confirmed_at"])
            self.assertTrue(vv["sign_path"].endswith(".png"))
        # 事件库留有 deal.ruled
        types = [e["type"] for e in self.svc.audit_feed(200)]
        self.assertIn("deal.ruled", types)

    def test_ruling_uses_frozen_rule_version_even_after_new_publish(self):
        deal = self._dispute()
        # 之后发布新版本，裁决仍按成交时冻结的 v1
        self.svc.publish_rule(
            "v2", {"protection_days": 15, "first_view_share": 80,
                   "last_view_share": 20,
                   "cross_store_split": {"bringing_agent": 50, "listing_store": 50}},
            "测试规则 v2", "B1")
        ruled = self.svc.rule_deal(
            deal["deal_id"], "B1",
            [{"agent_id": "A1", "share_pct": 50.0},
             {"agent_id": "A2", "share_pct": 50.0}], "按旧版裁决")
        self.assertEqual(ruled["ruling"]["rule_version"], "v1")


# ------------------------------------------------------------------ 并发双记录
class TestConcurrentSchedule(CaseBase):
    def test_concurrent_same_slot_creates_exactly_one_record(self):
        cid = self.customer(phone="13800000040")
        barrier = threading.Barrier(3)
        outcomes = []

        def attempt(agent):
            svc = Service(self.database,
                          SmsAdapter(self.database.conn(), echo=False))
            barrier.wait()
            try:
                svc.schedule_viewing(self.p1, cid, agent, "2026-10-15 09:00",
                                     ide_key=f"race-{agent}")
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")
            except DomainError:
                outcomes.append("denied")

        threads = [threading.Thread(target=attempt, args=(a,))
                   for a in ("A1", "A2", "B1")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count("ok"), 1, outcomes)
        self.assertEqual(outcomes.count("conflict"), 2, outcomes)
        rows = self.svc.rows(
            "SELECT COUNT(*) c FROM viewings WHERE property_id=? AND customer_id=?"
            " AND appointment_time=? AND status<>'cancelled'",
            (self.p1, cid, "2026-10-15 09:00:00"))
        self.assertEqual(rows[0]["c"], 1)

    def test_cancelled_slot_can_be_rescheduled(self):
        cid = self.customer(phone="13800000041")
        v1 = self.svc.schedule_viewing(
            self.p1, cid, "A1", "2026-10-16 09:00")["viewing_id"]
        self.svc.cancel_viewing(v1, "A1", "客户临时有事")
        # 取消释放槽位，可重新登记
        v2 = self.svc.schedule_viewing(
            self.p1, cid, "A2", "2026-10-16 09:00")["viewing_id"]
        self.assertNotEqual(v1, v2)
        active = self.svc.rows(
            "SELECT COUNT(*) c FROM viewings WHERE property_id=? AND customer_id=?"
            " AND appointment_time=? AND status<>'cancelled'",
            (self.p1, cid, "2026-10-16 09:00:00"))
        self.assertEqual(active[0]["c"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
