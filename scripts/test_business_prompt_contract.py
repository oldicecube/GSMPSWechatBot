"""Offline acceptance regressions, deliberately outside user-removed tests/."""
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import queue
from llm.core.response_parser import parse_proactive_response, parse_llm_response
from llm.core.llm_service import LLMService
from llm.learning import StyleLearner
from llm.memory import LongTermMemory
from llm.prompt.prompt_builder import (
    build_batch_user_messages,
    build_memory_curation_messages,
    build_reply_style_learning_messages,
)


class BusinessContract(unittest.TestCase):
    def test_json_output_contract(self):
        prompt = "\n".join(m["content"] for m in build_batch_user_messages({
            "batch_messages": [{"message_id": "s1", "content": "你好"}], "force_reply": True}))
        self.assertIn("array of integer batch_index", prompt)
        self.assertIn("never a message ID or null", prompt)
        bad = parse_proactive_response('{"should_reply":true,"reply_to":"s1","messages":["你好"]}', [])
        self.assertFalse(bad["_valid"])
        good = parse_proactive_response('{"should_reply":true,"reply_to":[1],"messages":["你好"],"animation":null}', [])
        self.assertEqual(good["messages"], ["你好"])

    def test_expression_learning_requires_current_sources(self):
        text = "\n".join(m["content"] for m in build_memory_curation_messages({
            "cycle_messages": [{"message_id": "h1", "content": "human evidence"}],
            "memory_state": {},
        }))
        self.assertIn("every expression_action other than keep", text)
        self.assertIn("source_ids", text)

    def test_expression_source_grounding_is_conservative(self):
        service = LLMService.__new__(LLMService)
        records = [{"source_id": "h1", "content": "修复成功，这下能安心了。", "role": "user"}]
        grounded, reason = service._ground_expression_action_in_cycle({
            "action": "add",
            "situation": "问题解决后",
            "pattern": "这下能安心了",
        }, records)
        self.assertEqual(reason, "ok")
        self.assertEqual(grounded["source_ids"], ["h1"])
        rejected, reason = service._ground_expression_action_in_cycle({
            "action": "add",
            "situation": "无关场景",
            "pattern": "不存在的句式",
        }, records)
        self.assertIsNone(rejected)
        self.assertEqual(reason, "expression_not_in_current_human_record")

    def test_hidden_history_is_excluded_at_final_renderer(self):
        text = "\n".join(m["content"] for m in build_batch_user_messages({
            "group_messages": [
                {"message_id": "h1", "content": "可见中文消息"},
                {"message_id": "h2", "content": "INTERNAL_ONLY_SECRET", "prompt_visible": False}],
            "batch_messages": [{"message_id": "b1", "content": "当前消息"}]}))
        self.assertIn("可见中文消息", text)
        self.assertNotIn("INTERNAL_ONLY_SECRET", text)

    def test_chinese_question_cleanup(self):
        raw = json.dumps({"messages": ["RC 是啥？可以再说说吗？"]}, ensure_ascii=False)
        self.assertEqual(parse_llm_response(raw, [])["messages"], ["RC 是啥？"])

    def test_temporary_learning_persistence_and_bot_exclusion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "learning.db")
            learner = StyleLearner({"learning": {"enabled": True, "db_path": path}}, start_worker=False)
            humans = [dict(group_id="g", content="修好了，这下能安心了。", speaker="a", speaker_name="a",
                           timestamp=1, fingerprint="h1", is_bot=False)]
            bot = dict(humans[0], fingerprint="b1", is_bot=True, content="BOT_ONLY")
            self.assertEqual(learner.ingest_many(humans + [bot]), 1)
            self.assertEqual(learner.apply_expression_actions("g", [dict(action="add", situation="修复成功",
                pattern="这下能安心了", source_ids=["h1"])], valid_source_ids=["h1"]), 1)
            reopened = StyleLearner({"learning": {"enabled": True, "db_path": path}}, start_worker=False)
            self.assertEqual(reopened.get_expressions("g")[0]["pattern"], "这下能安心了")
            learner.close()
            reopened.close()

    def test_person_fact_is_source_grounded_and_retrievable(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = LongTermMemory({"memory": {"enabled": True, "db_path": str(Path(folder) / "memory.db")}})
            memory.record_message(dict(group="g", wxid="a", content="我喜欢合作建造。", message_id="p1"))
            result = memory.apply_memory_actions("g", [dict(memory_type="person_fact", action="append",
                subject_id="a", fact_key="likes", content="喜欢合作建造", source_ids=["p1"])])
            self.assertEqual(result["added"], 1)
            self.assertIn("合作建造", memory.get_person_profile_context("g", "a", "喜欢"))
            self.assertNotIn("合作建造", memory.get_person_profile_context("other-group", "a", "喜欢"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
