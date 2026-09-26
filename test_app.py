import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ReviewStore


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.store = ReviewStore(self.db_path)
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

    def _decide_round_one(self, paper_id, reviewers=("r1", "r2")):
        assignments = []
        for reviewer in reviewers:
            aid = self.store.assign("chair", paper_id, reviewer)["id"]
            self.store.respond_assignment(reviewer, aid, True)
            self.store.submit_review(reviewer, aid, 4, "方法严谨，但缺少与最近工作的对比。")
            assignments.append(aid)
        self.store.decide("chair", paper_id, "reject", "现有证据不足以支撑结论。")
        return assignments

    def test_full_reconsideration_flow_old_reviews_cannot_decide(self):
        paper_id = self._paper()
        self._decide_round_one(paper_id)

        # 决定发布后，作者可附说明申请一次复核。
        req = self.store.request_reconsideration("alice", paper_id, "评审人 r1 与我存在未披露的同机构利益冲突。")
        self.assertEqual(req["status"], "pending")

        # 受理前论文仍是 decided；只有主席能受理。
        self.assertEqual(self.store.get_paper("alice", paper_id)["status"], "decided")
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_reconsideration("alice", paper_id, True)
        self.assertEqual(ctx.exception.status, 403)

        result = self.store.respond_reconsideration("chair", paper_id, True)
        self.assertEqual(result["status"], "accepted")
        paper = self.store.get_paper("chair", paper_id)
        self.assertEqual(paper["status"], "in_reconsideration")
        self.assertEqual(paper["review_round"], 2)

        # 原评审人已在第一轮分配过，不能再被安排为复核评审人。
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "assignment_exists")

        # 复核轮另行安排两名无冲突评审人；原轮评审不再支撑新决定。
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair", paper_id, "accept", "复核后接收。")
        self.assertEqual(ctx.exception.code, "insufficient_reviews")

        b3 = self.store.assign("chair", paper_id, "r3")
        self.assertEqual(b3["round"], 2)
        self.store.respond_assignment("r3", b3["id"], True)
        self.store.submit_review("r3", b3["id"], 5, "补充材料后工作扎实，建议接收。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide("chair", paper_id, "accept", "仍只有一份复核评审。")
        self.assertEqual(ctx.exception.code, "insufficient_reviews")

        b4 = self.store.assign("chair", paper_id, "r4")["id"]
        self.store.respond_assignment("r4", b4, True)
        self.store.submit_review("r4", b4, 4, "复核意见正面，小修后可接收。")
        decision = self.store.decide("chair", paper_id, "accept", "两轮评审后决定接收。")
        self.assertEqual(decision["round"], 2)
        self.assertEqual(self.store.get_paper("alice", paper_id)["status"], "decided")

        # 历史完整保留：原决定、原评审、复核各事件都在。
        history = self.store.history("chair", paper_id)
        actions = [h["action"] for h in history]
        self.assertEqual(actions.count("decision.record"), 2)
        self.assertIn("reconsideration.request", actions)
        self.assertIn("reconsideration.accept", actions)
        round_one = next(h for h in history if h["action"] == "decision.record" and h["detail"]["round"] == 1)
        round_two = [h for h in history if h["action"] == "decision.record" and h["detail"]["round"] == 2]
        self.assertEqual(len(round_two), 1)
        self.assertNotEqual(round_one["id"], round_two[0]["id"])

        # 已有新决定，不能重复开启复核。
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_reconsideration("alice", paper_id, "我们对第二轮决定仍然不服，希望再次申请复核。")
        self.assertEqual(ctx.exception.code, "reconsideration_already_opened")

    def test_only_decided_paper_can_request_and_only_author(self):
        paper_id = self._paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_reconsideration("alice", paper_id, "论文还没出决定，我先质疑一下评审安排。")
        self.assertEqual(ctx.exception.code, "paper_not_decided")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_reconsideration("bob", paper_id, "这不是我的论文但我也有意见需要说明。")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_reconsideration("alice", paper_id, "太短")
        self.assertEqual(ctx.exception.status, 422)

    def test_duplicate_pending_request_is_blocked(self):
        paper_id = self._paper()
        self._decide_round_one(paper_id)
        self.store.request_reconsideration("alice", paper_id, "评审存在明显的利益冲突，请主席复核。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_reconsideration("alice", paper_id, "我再补充说明一次我的复核诉求。")
        self.assertEqual(ctx.exception.code, "reconsideration_pending")
        # 待受理申请不能被受理两次。
        self.store.respond_reconsideration("chair", paper_id, True)
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_reconsideration("chair", paper_id, True)
        self.assertEqual(ctx.exception.code, "reconsideration_not_pending")

    def test_rejected_request_cannot_reopen_and_paper_stays_decided(self):
        paper_id = self._paper()
        self._decide_round_one(paper_id)
        self.store.request_reconsideration("alice", paper_id, "我怀疑评审人有利益冲突，请重新核查。")
        result = self.store.respond_reconsideration("chair", paper_id, False)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.store.get_paper("alice", paper_id)["status"], "decided")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_reconsideration("alice", paper_id, "申请被驳回了，我换个理由再试一次。")
        self.assertEqual(ctx.exception.code, "reconsideration_rejected")

    def test_conflicted_new_reviewer_is_blocked_in_round_two(self):
        paper_id = self._paper()
        self._decide_round_one(paper_id)
        self.store.request_reconsideration("alice", paper_id, "原评审存在利益冲突，请重新安排评审。")
        self.store.respond_reconsideration("chair", paper_id, True)
        self.store.add_conflict("chair", paper_id, "r3", "复核中新发现的合作关系")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r3")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")

    def test_legacy_schema_migrates_and_keeps_round_one(self):
        # 丢弃新库，先用旧版结构（无 round 列、旧 CHECK 约束）建库并产生一个决定。
        self.db_path.unlink(missing_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, load_limit INTEGER NOT NULL DEFAULT 3
            );
            CREATE TABLE papers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, author_id TEXT NOT NULL, title TEXT NOT NULL,
                abstract TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted'
                    CHECK (status IN ('submitted','under_review','decided','withdrawn')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id INTEGER NOT NULL, reviewer_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'invited',
                score INTEGER, review_text TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE (paper_id, reviewer_id)
            );
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id INTEGER NOT NULL UNIQUE,
                decision TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO users VALUES('alice','Alice','author',0)")
        conn.execute("INSERT INTO users VALUES('r3','R3','reviewer',3)")
        conn.execute("INSERT INTO users VALUES('r4','R4','reviewer',3)")
        conn.execute("INSERT INTO users VALUES('chair','Chair','chair',0)")
        conn.execute(
            "INSERT INTO papers(author_id,title,abstract,status,created_at) VALUES('alice','t','a','decided','now')"
        )
        conn.execute(
            "INSERT INTO decisions(paper_id,decision,decided_by,created_at) VALUES(1,'reject','chair','now')"
        )
        conn.commit()
        conn.close()

        # 重新初始化触发迁移：旧数据保留并归入第一轮。
        self.store.init_schema()
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT round FROM decisions WHERE paper_id=1").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT round FROM assignments").fetchall(), [])
        self.assertEqual(self.store.get_paper("chair", 1)["status"], "decided")

        # 迁移后复核全流程可用。
        self.store.request_reconsideration("alice", 1, "迁移后申请复核，确认旧库也支持新流程。")
        self.store.respond_reconsideration("chair", 1, True)
        a3 = self.store.assign("chair", 1, "r3")["id"]
        a4 = self.store.assign("chair", 1, "r4")["id"]
        self.store.respond_assignment("r3", a3, True)
        self.store.respond_assignment("r4", a4, True)
        self.store.submit_review("r3", a3, 4, "迁移验证用的复核评审意见。")
        self.store.submit_review("r4", a4, 5, "同意接收，迁移后流程正常。")
        new_decision = self.store.decide("chair", 1, "accept", "复核后接收。")
        self.assertEqual(new_decision["round"], 2)
        with self.store.connect() as conn:
            rounds = [r[0] for r in conn.execute("SELECT round FROM decisions ORDER BY id")]
        self.assertEqual(rounds, [1, 2])


if __name__ == "__main__":
    unittest.main()
