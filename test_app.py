import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        self.store.submit_rebuttal("alice", paper_id, "感谢意见，我们将补充对比并解释实验结论。")
        result = self.store.decide("chair", paper_id, "minor_revision", "补充实验后接收。")
        self.assertEqual(result["decision"], "minor_revision")
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.assertIsNotNone(self.store.get_paper("chair", paper_id)["author_id"])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        self.assertGreaterEqual(len(history), 8)

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("alice", paper_id, "r2")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_paper("r2", paper_id)
        self.assertEqual(ctx.exception.status, 403)


class RereviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _decided_paper(self):
        paper_id = self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 2, "评审人与作者存在合作，意见明显偏颇。")
        self.store.submit_review("r2", a2, 2, "审稿时间不足，意见简略但已按要求完成。")
        self.store.decide("chair", paper_id, "reject", "根据两份评审拒稿。")
        return paper_id

    def test_rereview_full_flow(self):
        paper_id = self._decided_paper()
        req = self.store.request_rereview("alice", paper_id, "评审人 r1 与作者同实验室，存在明显利益冲突。")
        self.assertEqual(req["status"], "pending")
        # 每篇论文只能申请一次复核。
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_rereview("alice", paper_id, "重复提交复核申请说明。")
        self.assertEqual(ctx.exception.code, "rereview_exists")

        result = self.store.respond_rereview("chair", paper_id, True)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["round"], 2)
        self.assertEqual(self.store.get_paper("chair", paper_id)["status"], "re_review")

        # 原有评审不能支撑新决定。
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair", paper_id, "accept", "试图沿用原评审。")
        self.assertEqual(ctx.exception.code, "insufficient_reviews")

        # 复核轮仍执行利益冲突检查。
        self.store.add_conflict("chair", paper_id, "r1", "与作者同一实验室")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")

        # 新一轮安排两名无冲突评审人并完成评审。
        a3 = self.store.assign("chair", paper_id, "r3")["id"]
        a4 = self.store.assign("chair", paper_id, "r4")["id"]
        self.store.respond_assignment("r3", a3, True)
        self.store.respond_assignment("r4", a4, True)
        self.store.submit_review("r3", a3, 4, "独立复核：方法扎实，建议接收。")
        self.store.submit_review("r4", a4, 5, "独立复核：实验充分， strongly recommend。")
        result = self.store.decide("chair", paper_id, "accept", "复核后改判接收。")
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(self.store.get_paper("chair", paper_id)["status"], "decided")

        # 原决定、原评审、原分配都留在历史里。
        history = self.store.history("chair", paper_id)
        actions = [h["action"] for h in history]
        self.assertEqual(actions.count("decision.record"), 2)
        self.assertEqual(actions.count("review.submit"), 4)
        self.assertEqual(actions.count("assignment.invite"), 4)
        self.assertIn("rereview.request", actions)
        self.assertIn("rereview.respond", actions)
        first_decision = next(h for h in history if h["action"] == "decision.record")
        self.assertEqual(first_decision["detail"]["decision"], "reject")
        self.assertEqual(first_decision["detail"]["round"], 1)

        # 已有新决定，不能重复开启复核。
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_rereview("chair", paper_id, True)
        self.assertEqual(ctx.exception.code, "rereview_already_answered")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_rereview("alice", paper_id, "对新决定再次申请复核。")
        self.assertEqual(ctx.exception.code, "rereview_exists")

    def test_rereview_reject_keeps_decision(self):
        paper_id = self._decided_paper()
        self.store.request_rereview("alice", paper_id, "评审人 r1 与作者存在项目合作。")
        result = self.store.respond_rereview("chair", paper_id, False)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.store.get_paper("chair", paper_id)["status"], "decided")
        # 未接受申请，不能开启复核：论文不可重新分配，也不能重复处理申请。
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r3")
        self.assertEqual(ctx.exception.code, "paper_unavailable")
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_rereview("chair", paper_id, True)
        self.assertEqual(ctx.exception.code, "rereview_already_answered")

    def test_rereview_request_validation(self):
        paper_id = self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]
        # 未决定的论文不能申请复核。
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_rereview("alice", paper_id, "论文尚未决定，提前申请复核。")
        self.assertEqual(ctx.exception.code, "paper_not_decided")
        decided_id = self._decided_paper()
        # 说明太短、非本人论文、非作者角色分别被拒绝。
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_rereview("alice", decided_id, "太短")
        self.assertEqual(ctx.exception.code, "reason_too_short")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_rereview("bob", decided_id, "不是本人的论文不能申请。")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_rereview("chair", decided_id, "主席不能代替作者申请复核。")
        self.assertEqual(ctx.exception.status, 403)
        # 不存在的申请不能处理。
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_rereview("chair", decided_id, True)
        self.assertEqual(ctx.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
