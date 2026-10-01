"""领域规则测试：保护期边界、客户去重、跨店权限、裁决留痕、并发防双记录等。"""
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta

from app import db as store
from app import domain as D


# 30 天 / 60 天两版规则，与种子一致
RULE_V1 = {"window_days": 30,
           "sole_first": {"viewer_share": 0.80},
           "same_day": {"home_store_share_when_cross": 0.20},
           "cross_store": {"home_store_share": 0.30},
           "no_protection": {"home_store_share": 1.0}}
RULE_V2 = {"window_days": 60,
           "sole_first": {"viewer_share": 0.70},
           "same_day": {"home_store_share_when_cross": 0.25},
           "cross_store": {"home_store_share": 0.35},
           "no_protection": {"home_store_share": 1.0}}

PNG = ("data:image/png;base64,"
       "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
       "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        store.init_db(self.db_path)
        self.conn = store.get_conn(self.db_path)
        self._fixtures()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _fixtures(self):
        c = self.conn
        ts = store.now_iso()
        c.execute("INSERT INTO stores(id,code,name,created_ts) VALUES (1,'S1','中山店',?)", (ts,))
        c.execute("INSERT INTO stores(id,code,name,created_ts) VALUES (2,'S2','东港店',?)", (ts,))
        agents = [
            (1, 1, "A1", "王梅", "1390001", "agent", 1),   # 可跨店
            (2, 1, "A2", "李强", "1390002", "agent", 0),   # 不可跨店
            (3, 1, "A3", "周敏", "1390003", "agent", 1),
            (4, 2, "A4", "赵航", "1390004", "agent", 1),
            (5, 1, "M1", "陈岚主管", "1390099", "supervisor", 1),
            (6, 2, "M2", "孙岩主管", "1390098", "supervisor", 1),
        ]
        c.executemany(
            "INSERT INTO agents(id,store_id,employee_no,name,phone,role,can_cross_store,active)"
            " VALUES (?,?,?,?,?,?,?,1)", agents)
        import json
        c.execute("INSERT INTO rule_versions(id,version,title,effective_from,body_json,created_by,published_ts)"
                  " VALUES (1,'2025-v1','v1','2025-01-01',?,5,?)",
                  (json.dumps(RULE_V1), ts))
        c.execute("INSERT INTO rule_versions(id,version,title,effective_from,body_json,created_by,published_ts)"
                  " VALUES (2,'2026-v2','v2','2026-09-01',?,5,?)",
                  (json.dumps(RULE_V2), ts))
        for i, sid in enumerate((1, 1, 2), start=1):
            c.execute("INSERT INTO properties(id,store_id,address,district,price,status,listed_ts)"
                      " VALUES (?,?,?,?,?,'on','2026-01-01T00:00:00Z')",
                      (i, sid, f"地址{i}", "区", 1000000))

    def client(self, name="客户", phone=None, idno=None):
        c, _ = D.get_or_create_client(self.conn, name, phone, idno, 1)
        return c["id"]

    def view(self, pid, cid, when, agent=None, self_visit=False, cross=False, check=True,
             confirm=True):
        vid, _ = D.register_viewing(self.conn, cid, pid, when, agent_id=agent,
                                    is_self_visit=self_visit, cross_store_requested=cross,
                                    actor=agent or 1)
        if check:
            kw = {}
            if not self_visit:
                kw["sig_agent_data"] = PNG
            D.check_in(self.conn, vid, sig_client_data=PNG,
                       client_confirmed=confirm, **kw)
        return vid


class TestProtectionBoundary(Case):
    def test_60d_window_v2_includes_last_day(self):
        cid = self.client(phone="1300001")
        self.view(1, cid, "2026-09-01T10:00", agent=1)
        # 到期日当天（9/1 + 60 = 10/31）仍在保护期
        st = D.protection_status(self.conn, cid, 1, "2026-10-31")
        self.assertTrue(st["protected"])
        self.assertEqual(st["until"], "2026-10-31")
        self.assertEqual(st["window_days"], 60)
        self.assertEqual(st["rule_version"], "2026-v2")
        # 次日失效
        st2 = D.protection_status(self.conn, cid, 1, "2026-11-01")
        self.assertFalse(st2["protected"])

    def test_30d_window_v1(self):
        cid = self.client(phone="1300002")
        self.view(1, cid, "2026-08-01T10:00", agent=1)
        st = D.protection_status(self.conn, cid, 1, "2026-08-31")  # +30 天当天
        self.assertTrue(st["protected"])
        self.assertEqual(st["rule_version"], "2025-v1")
        self.assertFalse(D.protection_status(self.conn, cid, 1, "2026-09-01")["protected"])

    def test_rule_version_switches_sept1(self):
        # 8/31 成交适用 v1，9/1 起适用 v2
        self.assertEqual(D.rule_for_date(self.conn, "2026-08-31")["version"], "2025-v1")
        self.assertEqual(D.rule_for_date(self.conn, "2026-09-01")["version"], "2026-v2")

    def test_deal_after_window_goes_to_home_store(self):
        cid = self.client(phone="1300003")
        self.view(1, cid, "2026-08-01T10:00", agent=1)
        deal_id, res = D.record_deal(self.conn, 1, cid, "2026-09-15", 1000000, 20000)
        self.assertFalse(res["status"]["protected"])
        self.assertEqual(len(res["candidates"]), 1)
        self.assertIsNone(res["candidates"][0]["agent_id"])
        self.assertEqual(res["candidates"][0]["suggested_share"], 1.0)
        self.assertFalse(res["needs_dispute"])

    def test_self_visit_only_no_protection(self):
        cid = self.client(phone="1300004")
        self.view(1, cid, "2026-09-10T10:00", self_visit=True)
        st = D.protection_status(self.conn, cid, 1, "2026-09-20")
        self.assertFalse(st["protected"])
        deal_id, res = D.record_deal(self.conn, 1, cid, "2026-09-20", 1000000, 20000)
        self.assertEqual(res["candidates"][0]["role_label"], "self_visit")


class TestDedup(Case):
    def test_same_phone_merges(self):
        a, created_a = D.get_or_create_client(self.conn, "张三", "1381111", None, 1)
        b, created_b = D.get_or_create_client(self.conn, "张三（展会名）", "1381111", None, 1)
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(created_a)
        self.assertFalse(created_b)
        aliases = self.conn.execute(
            "SELECT kind FROM client_aliases WHERE client_id=?", (a["id"],)).fetchall()
        self.assertIn("phone", [r["kind"] for r in aliases])

    def test_same_idno_merges_even_different_phone(self):
        a, _ = D.get_or_create_client(self.conn, "李四", "1382222", "ID-X1", 1)
        b, _ = D.get_or_create_client(self.conn, "李四二", "1383333", "ID-X1", 1)
        self.assertEqual(a["id"], b["id"])

    def test_distinct_phones_are_distinct_clients(self):
        a, _ = D.get_or_create_client(self.conn, "王芳", "1384444", None, 1)
        b, _ = D.get_or_create_client(self.conn, "王芳", "1385555", None, 1)
        self.assertNotEqual(a["id"], b["id"])

    def test_find_duplicates(self):
        D.get_or_create_client(self.conn, "钱七", "1386666", None, 1)
        D.get_or_create_client(self.conn, "钱七（别名）", "1386666", None, 1)
        hits = D.find_duplicates(self.conn, "1386666")
        self.assertEqual(len(hits), 1)  # 合并后只剩一个档案


class TestCrossStore(Case):
    def test_agent_without_cross_store_permission_blocked(self):
        cid = self.client(phone="1300101")
        with self.assertRaises(D.PermissionError_):
            D.register_viewing(self.conn, cid, 3, "2026-09-10T10:00", agent_id=2,
                               cross_store_requested=True)

    def test_cross_store_requires_explicit_confirm(self):
        cid = self.client(phone="1300102")
        with self.assertRaises(D.PermissionError_):
            D.register_viewing(self.conn, cid, 3, "2026-09-10T10:00", agent_id=1,
                               cross_store_requested=False)

    def test_cross_store_ok_with_permission_and_confirm(self):
        cid = self.client(phone="1300103")
        vid, _ = D.register_viewing(self.conn, cid, 3, "2026-09-10T10:00", agent_id=1,
                                    cross_store_requested=True)
        v = self.conn.execute("SELECT * FROM viewings WHERE id=?", (vid,)).fetchone()
        self.assertEqual(v["cross_store"], 1)

    def test_cross_store_exclusive_split(self):
        cid = self.client(phone="1300104")
        self.view(3, cid, "2026-09-10T10:00", agent=1, cross=True)
        deal_id, res = D.record_deal(self.conn, 3, cid, "2026-09-20", 1000000, 20000)
        shares = {c["role_label"]: c["suggested_share"] for c in res["candidates"]}
        self.assertAlmostEqual(shares["cross_store"], 0.65)   # v2: 协作经纪人
        self.assertAlmostEqual(shares["home_store"], 0.35)


class TestSameDayConflict(Case):
    def test_three_agents_same_day_split_and_dispute(self):
        cid = self.client(phone="1300201")
        self.view(1, cid, "2026-09-20T10:00", agent=1)
        self.view(1, cid, "2026-09-20T11:00", agent=3)
        self.view(1, cid, "2026-09-20T14:00", agent=4, cross=True)
        deal_id, res = D.record_deal(self.conn, 1, cid, "2026-09-28", 1000000, 20000)
        self.assertTrue(res["needs_dispute"])
        by_label = {}
        for c in res["candidates"]:
            by_label.setdefault(c["role_label"], []).append(c["suggested_share"])
        self.assertAlmostEqual(by_label["home_store"][0], 0.25)
        self.assertEqual(len(by_label["same_day"]) + len(by_label.get("cross_store", [])), 3)
        total = sum(c["suggested_share"] for c in res["candidates"])
        self.assertAlmostEqual(total, 1.0)
        # 自动开争议
        d = self.conn.execute("SELECT * FROM disputes WHERE deal_id=?", (deal_id,)).fetchone()
        self.assertIsNotNone(d)
        self.assertEqual(d["status"], "open")


class TestDuplicateRegistration(Case):
    def test_concurrent_same_viewing_single_record(self):
        cid = self.client(phone="1300301")
        errors, ok = [], []
        start = threading.Event()

        def attempt(i):
            conn = store.get_conn(self.db_path)  # 每线程独立连接
            start.wait()
            try:
                vid, _ = D.register_viewing(conn, cid, 1, "2026-09-25T10:00", agent_id=2,
                                            idem_key=f"concurrent-{i}")
                ok.append(vid)
            except D.DomainError as e:
                errors.append(str(e))
            finally:
                conn.close()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join()
        self.assertEqual(len(ok), 1, f"应只成功一条，实际成功 {len(ok)}")
        self.assertEqual(len(errors), 7)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM viewings WHERE client_id=? AND property_id=? AND agent_id=2"
            " AND status!='cancelled'", (cid, 1)).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_cancelled_releases_slot_but_checkin_blocks_cancel(self):
        cid = self.client(phone="1300302")
        vid, _ = D.register_viewing(self.conn, cid, 1, "2026-09-25T09:00", agent_id=2)
        D.cancel_viewing(self.conn, vid, reason="改期", actor=2)
        # 取消后同日可重新登记
        vid2, _ = D.register_viewing(self.conn, cid, 1, "2026-09-25T15:00", agent_id=2)
        self.assertNotEqual(vid, vid2)
        # 未签到的预约可以正常取消，取消后仍可再次登记
        D.cancel_viewing(self.conn, vid2, reason="再次改期", actor=2)
        vid3, _ = D.register_viewing(self.conn, cid, 1, "2026-09-25T18:00", agent_id=2)
        # 已签到不能取消
        D.check_in(self.conn, vid3, sig_agent_data=PNG, sig_client_data=PNG,
                   client_confirmed=True)
        with self.assertRaises(D.DomainError):
            D.cancel_viewing(self.conn, vid3, actor=2)

    def test_double_checkin_rejected(self):
        cid = self.client(phone="1300303")
        vid = self.view(1, cid, "2026-09-25T10:00", agent=1)
        with self.assertRaises(D.DomainError):
            D.check_in(self.conn, vid, sig_agent_data=PNG, sig_client_data=PNG,
                       client_confirmed=True)


class TestAdjudication(Case):
    def _open_dispute(self):
        cid = self.client(phone="1300401")
        self.view(1, cid, "2026-09-20T10:00", agent=1, confirm=True)
        self.view(1, cid, "2026-09-20T11:00", agent=3, confirm=True)
        deal_id, _ = D.record_deal(self.conn, 1, cid, "2026-09-28", 1000000, 20000)
        did = self.conn.execute(
            "SELECT id FROM disputes WHERE deal_id=?", (deal_id,)).fetchone()["id"]
        return did

    def test_agent_cannot_decide_own_dispute(self):
        did = self._open_dispute()
        with self.assertRaises(D.PermissionError_):
            D.decide_dispute(self.conn, did, 1, {}, "x", True)

    def test_non_supervisor_role_rejected(self):
        did = self._open_dispute()
        with self.assertRaises(D.PermissionError_):
            D.decide_dispute(self.conn, did, 2, {}, "x", True)

    def test_missing_client_confirmation_blocks(self):
        cid = self.client(phone="1300402")
        self.view(1, cid, "2026-09-20T10:00", agent=1, confirm=True)
        self.view(1, cid, "2026-09-20T11:00", agent=3, confirm=False)  # 缺确认
        deal_id, _ = D.record_deal(self.conn, 1, cid, "2026-09-28", 1000000, 20000)
        did = self.conn.execute("SELECT id FROM disputes WHERE deal_id=?", (deal_id,)).fetchone()["id"]
        d = D.get_dispute(self.conn, did)
        gaps = D.missing_confirmation(self.conn, d)
        self.assertTrue(any(r == "缺少客户确认" for r, _ in gaps))
        outcome = {c["id"]: 1 / len(d["candidates"]) for c in d["candidates"]}
        with self.assertRaises(D.DomainError):
            D.decide_dispute(self.conn, did, 5, outcome, "补齐后再说", True)
        # 主管补录确认后可裁决
        unconfirmed = [e["viewing_id"] for r, e in gaps if r == "缺少客户确认"]
        for vid in unconfirmed:
            D.record_client_confirmation(self.conn, vid, actor=5)
        D.decide_dispute(self.conn, did, 5, outcome, "已电话核对", True)

    def test_evidence_reviewed_flag_required(self):
        did = self._open_dispute()
        d = D.get_dispute(self.conn, did)
        outcome = {c["id"]: 1 / len(d["candidates"]) for c in d["candidates"]}
        with self.assertRaises(D.DomainError):
            D.decide_dispute(self.conn, did, 5, outcome, "未核对", False)

    def test_shares_must_sum_to_one(self):
        did = self._open_dispute()
        d = D.get_dispute(self.conn, did)
        outcome = {c["id"]: 0.4 for c in d["candidates"]}
        with self.assertRaises(D.DomainError):
            D.decide_dispute(self.conn, did, 5, outcome, "合计不对", True)

    def test_decision_is_recorded_with_rule_version_and_immutable(self):
        did = self._open_dispute()
        d = D.get_dispute(self.conn, did)
        outcome = {c["id"]: round(1 / len(d["candidates"]), 4) for c in d["candidates"]}
        # 修正合计误差到第一条
        first = d["candidates"][0]["id"]
        outcome[first] = round(1 - sum(v for k, v in outcome.items() if k != first), 4)
        D.decide_dispute(self.conn, did, 5, outcome,
                         "核对 2026-v2、签到与客户确认", True, actor=5)
        d2 = D.get_dispute(self.conn, did)
        self.assertEqual(d2["status"], "decided")
        self.assertEqual(d2["decision"]["rule_version_id"], d2["rule"]["id"])
        self.assertEqual(d2["rule"]["version"], "2026-v2")
        self.assertEqual(d2["decision"]["decider"]["id"], 5)
        self.assertTrue(d2["decision"]["evidence_reviewed"])
        # 事件留痕
        ev = store.load_events(self.conn, "dispute_decided")
        self.assertEqual(ev[-1]["payload"]["rule_version"], "2026-v2")
        self.assertEqual(ev[-1]["payload"]["decided_by"], 5)
        self.assertTrue(ev[-1]["payload"]["evidence_reviewed"])
        # 不可二次裁决
        with self.assertRaises(D.DomainError):
            D.decide_dispute(self.conn, did, 5, outcome, "改判", True)

    def test_decision_event_chain_present(self):
        did = self._open_dispute()
        types = {e["event_type"] for e in store.load_events(self.conn)}
        self.assertIn("dispute_opened", types)
        self.assertIn("deal_recorded", types)


class TestProtectionWarningSms(Case):
    def test_second_agent_triggers_sms_to_holder(self):
        cid = self.client(phone="1300501")
        self.view(1, cid, "2026-09-20T10:00", agent=1)
        _, warn = D.register_viewing(self.conn, cid, 1, "2026-10-03T09:00", agent_id=3)
        self.assertIsNotNone(warn)
        self.assertEqual(warn["agent_id"], 1)
        n = self.conn.execute("SELECT COUNT(*) c FROM sms_log WHERE phone='1390001'").fetchone()["c"]
        self.assertGreaterEqual(n, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
