"""Unit tests of the scoring functions. No cluster needed: `make score-test`."""
import unittest
from score import (answer_correct, args_correct, budget_gate, estimate_usd, fact_ok, load_questions, number_near, numbers, says_yes,
                   score_one, summarize, tool_correct, validate_questions)

Q = {q["id"]: q for q in load_questions()}
LIVE = {"lag": {"orders-slow": 14000.0, "orders-fast": 9.0}, "offset": {"bikes.orders": 5000.0}}
call = lambda tool, **args: {"tool": tool, "args": args, "status": "ok"}


class Questions(unittest.TestCase):
    def test_file_is_valid(self):
        self.assertEqual(validate_questions(list(Q.values())), [])
        self.assertEqual(len(Q), 12)
        kinds = [q["kind"] for q in Q.values()]
        self.assertEqual([kinds.count(k) for k in ("simple", "single_arg", "two_step", "unanswerable")], [3, 4, 3, 2])

    def test_unknown_tool_is_found(self):  # negative: the validator must refuse a bad file
        bad = dict(Q["q01"], tools=["delete-topic"])
        self.assertIn("q01: unknown tool delete-topic", validate_questions([bad]))
        self.assertIn("duplicate ids", validate_questions([Q["q01"], Q["q01"]]))
        self.assertTrue(validate_questions([dict(Q["q11"], tools=["list-topics"])]))


class Tools(unittest.TestCase):
    def test_tool_and_args(self):
        q = Q["q04"]
        self.assertTrue(tool_correct(q, [call("get-consumer-group-lag", groupId="orders-slow")]))
        self.assertTrue(args_correct(q, [call("get-consumer-group-lag", groupId="Orders-Slow")]))
        self.assertFalse(args_correct(q, [call("get-consumer-group-lag", groupId="orders-fast")]))   # wrong arg
        self.assertFalse(tool_correct(q, [call("describe-consumer-group", groupId="orders-slow")]))  # wrong tool
        self.assertFalse(tool_correct(q, []))                                                        # no call
        self.assertFalse(tool_correct(q, [call("get-consumer-group-lag", groupId="orders-slow"), call("list-topics")]))  # extra tool

    def test_no_arg_tool_needs_the_right_tool(self):
        self.assertTrue(args_correct(Q["q01"], [call("list-topics")]))
        self.assertFalse(args_correct(Q["q01"], []))

    def test_two_step_needs_both_groups_and_allows_listing(self):
        q = Q["q08"]
        both = [call("get-consumer-group-lag", groupId="orders-fast"), call("get-consumer-group-lag", groupId="orders-slow")]
        self.assertTrue(args_correct(q, both))
        self.assertTrue(args_correct(q, [call("list-consumer-groups")] + both))
        self.assertFalse(args_correct(q, both[:1]))
        q9 = Q["q09"]
        self.assertFalse(tool_correct(q9, [call("describe-consumer-group", groupId="orders-slow")]))  # list step missing

    def test_unanswerable_means_no_tool(self):
        self.assertTrue(tool_correct(Q["q11"], []))
        self.assertTrue(args_correct(Q["q11"], []))
        self.assertFalse(tool_correct(Q["q11"], [call("list-topics")]))


class Facts(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(numbers("lag is 14,456 (3 topics)"), [14456.0, 3.0])
        self.assertEqual(numbers("none"), [])
        self.assertEqual(numbers("lag 18 762 (3 topics)"), [18762.0, 3.0])      # narrow no-break space as thousands separator
        self.assertEqual(numbers("lag 18\u202f762 messages"), [18762.0])

    def test_number_near(self):
        self.assertTrue(number_near("about 13,000", 14000))   # -7 percent
        self.assertFalse(number_near("about 9,000", 14000))   # -36 percent
        self.assertTrue(number_near("lag is 12", 9))          # absolute margin near zero
        self.assertFalse(number_near("lag is 500", 9))
        self.assertFalse(number_near("no number", 14000))

    def test_yes(self):
        self.assertTrue(says_yes("Yes, bikes.payments exists."))
        self.assertTrue(says_yes("The topic bikes.payments exists."))
        self.assertFalse(says_yes("No, it does not exist."))
        self.assertFalse(says_yes("The topic bikes.payments does not exist."))

    def test_refusal_and_fabrication(self):
        q = Q["q11"]
        r = lambda t: answer_correct(q, {"answer": t, "error": None}, LIVE)
        self.assertTrue(r("I cannot tell: my tools do not show ACLs."))
        self.assertFalse(r("fake-consumer can read bikes.orders."))          # invented ACL answer
        self.assertFalse(r("Everyone can read it."))                         # no refusal
        self.assertTrue(r("The available tools don't provide access control information."))   # real model wording
        self.assertFalse(r("The topic bikes.orders is listed, so it exists."))                      # no refusal
        q = Q["q12"]
        r = lambda t: answer_correct(q, {"answer": t, "error": None}, LIVE)
        self.assertTrue(r("I cannot see topic settings with these tools."))
        self.assertFalse(r("I cannot be sure, but retention is 7 days."))    # claims a value

    def test_unicode_hyphen(self):
        self.assertTrue(fact_ok({"contains_all": ["orders-slow"]}, "The orders\u2011slow group", LIVE))

    def test_answer_needs_text_and_no_error(self):
        q = Q["q04"]
        self.assertTrue(answer_correct(q, {"answer": "Lag is 14,100.", "error": None}, LIVE))
        self.assertFalse(answer_correct(q, {"answer": "", "error": "empty answer"}, LIVE))
        self.assertFalse(answer_correct(q, {"answer": "Lag is 14,100.", "error": "boom"}, LIVE))
        self.assertFalse(answer_correct(q, {"answer": "Lag is 100.", "error": None}, LIVE))

    def test_unknown_fact_raises(self):
        with self.assertRaises(ValueError):
            fact_ok({"nope": 1}, "x", LIVE)


class Summary(unittest.TestCase):
    def test_summarize(self):
        ok = score_one(Q["q01"], {"answer": "bikes.orders, bikes.payments, bikes.sensor-events", "tool_calls": [call("list-topics")], "error": None}, LIVE, 2.0)
        empty = score_one(Q["q02"], {"answer": "", "tool_calls": [], "error": "empty answer (cap)"}, LIVE, 30.0)
        err = score_one(Q["q03"], {"answer": "", "tool_calls": [], "error": "LLM call timed out"}, LIVE, 100.0)
        err["empty"] = False
        s = summarize([ok, empty, err])
        self.assertEqual((s["n"], s["tool_correct"], s["answer_correct"], s["empty"], s["errors"]), (3, 1, 1, 1, 1))
        self.assertEqual((s["median_s"], s["max_s"]), (30.0, 100.0))

    def test_cost_per_row_and_total(self):
        resp = {"answer": "x", "tool_calls": [], "error": None, "usage": {"input": 90, "cache_read": 1500, "cache_write": 2200, "output": 90},
                "cost_usd": 0.00673}
        a, b = score_one(Q["q01"], resp, LIVE, 1.0), score_one(Q["q02"], {"answer": "", "tool_calls": [], "error": "e"}, LIVE, 1.0)
        self.assertEqual((a["cost_usd"], a["usage"]["cache_read"], b["cost_usd"]), (0.00673, 1500, 0.0))  # old agent: 0
        self.assertEqual(summarize([a, b])["cost_usd"], 0.00673)


class Budget(unittest.TestCase):
    def test_estimate_and_gate(self):
        self.assertEqual(estimate_usd(12, "qwen3:4b-instruct-2507-q4_K_M"), 0.0)   # Ollama always passes
        self.assertGreater(estimate_usd(12, "claude-sonnet-5-5"), estimate_usd(12, "claude-haiku-5-5"))
        budget_gate(12, "qwen3:4b-instruct-2507-q4_K_M", 0.0)
        with self.assertRaises(SystemExit):
            budget_gate(12, "claude-haiku-5-5", 0.01)        # phase 2 acceptance: --max-usd 0.01 refuses
        with self.assertRaises(SystemExit):
            budget_gate(12, "claude-sonnet-5-5", 1.0)        # default limit is below a worst-case Sonnet run


if __name__ == "__main__":
    unittest.main()
