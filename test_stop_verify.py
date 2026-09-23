#!/usr/bin/env python3
"""Offline checks for the hook's decision logic. No network: the judge is a fake that answers
from a table, and the transcript is built here. Run: python3 test_stop_verify.py"""
import io, json, os, sys, tempfile, unittest, pathlib
from unittest import mock

os.environ.setdefault("VERIFIER_BACKEND", "jev")
import stop_verify as s


def transcript(turns):
    """turns: list of (user_text, [(tool_command, tool_output)], assistant_answer_or_None)."""
    rows, n = [], 0
    for user_text, tools, answer in turns:
        rows.append({"type": "user", "message": {"content": user_text}})
        for cmd, out in tools:
            n += 1
            rows.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{n}", "input": {"command": cmd}}]}})
            rows.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": out}]}})
        if answer:
            rows.append({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    f.write("\n".join(json.dumps(r) for r in rows)); f.close()
    return f.name


def fake_judge(pass1=None, pass2=None):
    """pass1: {sentence_substring: (choice, mutable_noul)}; pass2: {substring: (supported, contradicted, not_addressed, confidence)}."""
    def judge(state, questions):
        answers = {}
        for qid, q in questions.items():
            text = q["instructions"]
            if qid.startswith("m"):
                p = next((v[1] for k, v in (pass1 or {}).items() if k in text), 0.0)
                answers[qid] = {"type": "noul", "noul": p}
            elif q["type"] == "choice" and "fact_about_existing_code" in q.get("criteria", {}):
                choice = next((v[0] for k, v in (pass1 or {}).items() if k in text), "other")
                answers[qid] = {"type": "choice", "choice": choice,
                                "probabilities": {c: (1.0 if c == choice else 0.0) for c in q["criteria"]}}
            else:  # pass 2, batched: claim text lives in the state
                claim = state["claims"][qid]["claim"] if "claims" in state else state["claim"]
                sp, cp, np_, conf = next((v for k, v in (pass2 or {}).items() if k in claim), (0.2, 0.1, 0.7, 0.9))
                answers[qid] = {"type": "choice", "choice": max({"supported": sp, "contradicted": cp, "not_addressed": np_}, key=lambda k: {"supported": sp, "contradicted": cp, "not_addressed": np_}[k]),
                                "probabilities": {"supported": sp, "contradicted": cp, "not_addressed": np_}, "confidence": conf}
        return answers
    return judge


def run_hook(path, judge):
    out = io.StringIO()
    with mock.patch.dict(s.BACKENDS, {s.VERIFIER_BACKEND: judge}), \
         mock.patch.object(s, "LOG", pathlib.Path(tempfile.mkstemp()[1])), \
         mock.patch.object(s, "SENT", pathlib.Path(tempfile.mkstemp()[1])), \
         mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": "test", "transcript_path": path}))), \
         mock.patch.object(sys, "stdout", out):
        s.main()
    return json.loads(out.getvalue()) if out.getvalue().strip() else None


PADDING = "x " * 110  # answers shorter than 200 chars aren't picked up as the final answer
CLAIM = "The pull request number 4 is still open and waiting for a merge right now."


class Stale(unittest.TestCase):
    def test_mutable_claim_with_old_evidence_blocks(self):
        turns = [("check the pr", [("gh pr view 4 --json state", "PR #4 (stun-nat): OPEN")], "Checked it. " + PADDING)]
        turns += [("ok", [("ls", "README.md")], "Fine. " + PADDING)] * s.STALE_TURNS
        turns += [("status?", [("ls", "README.md")], CLAIM + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass1={CLAIM: ("about_this_conversation", 0.9)}))
        self.assertIsNotNone(r); self.assertIn("[STALE] " + CLAIM, r["reason"])

    def test_mutable_claim_with_fresh_evidence_passes(self):
        turns = [("status?", [("gh pr view 4 --json state", "PR #4 (stun-nat): OPEN")], CLAIM + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass1={CLAIM: ("about_this_conversation", 0.9)}))
        self.assertIsNone(r)

    def test_proposal_is_exempt(self):
        turns = [("ok", [("ls", "README.md")], "Fine. " + PADDING)] * (s.STALE_TURNS + 1)
        turns += [("plan?", [("ls", "README.md")], CLAIM + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass1={CLAIM: ("proposal_or_opinion", 0.95)}))
        self.assertIsNone(r)

    def test_below_mutable_threshold_passes(self):
        turns = [("ok", [("ls", "README.md")], "Fine. " + PADDING)] * (s.STALE_TURNS + 1)
        turns += [("status?", [("ls", "README.md")], CLAIM + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass1={CLAIM: ("about_this_conversation", s.MUTABLE - 0.05)}))
        self.assertIsNone(r)


class Verdicts(unittest.TestCase):
    def test_contradicted_blocks_only_with_keyword_overlap(self):
        c1 = "The server binds to every interface on port 8787 by default."
        c2 = "Paris is the capital city of France according to the source."
        turns = [("check", [("grep HOST laya_server.py", 'HOST = os.environ.get("LAYA_HOST", "127.0.0.1")  # server binds port')], f"{c1} {c2} " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass2={c1: (0.05, 0.9, 0.05, 0.9), c2: (0.05, 0.9, 0.05, 0.9)}))
        self.assertIn("[CONTRADICTED] " + c1, r["reason"])
        self.assertNotIn(c2, r["reason"])  # no shared keyword with any evidence → logged, not blocked

    def test_low_confidence_verdict_never_blocks(self):
        c1 = "The server binds to every interface on port 8787 by default."
        turns = [("check", [("grep HOST laya_server.py", "HOST = 127.0.0.1  # server binds port")], c1 + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass2={c1: (0.05, 0.9, 0.05, s.FIRM - 0.1)}))
        self.assertIsNone(r)

    def test_unsupported_fact_with_no_relevant_evidence_blocks(self):
        c1 = "The scheduler retries failed jobs three times before giving up."
        turns = [("check", [("ls", "README.md\nsetup.py")], c1 + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass1={c1: ("fact_about_existing_code", 0.0)}, pass2={c1: (0.05, 0.05, 0.9, 0.9)}))
        self.assertIn("[UNSUPPORTED] " + c1, r["reason"])


FACT_CLAIM = "The scheduler retries failed jobs three times before giving up on them."


class Sources(unittest.TestCase):
    def test_claim_backed_only_by_subagent_report_blocks_as_relayed(self):
        report = ("Another Claude session sent a message:\n<agent-message from=\"abc123\">\n[Subagent hand-back] The report follows:\n"
                  "  The scheduler retries failed jobs three times before giving up, see scheduler.rs:40\n</agent-message>")
        turns = [("look", [("ls", "README.md")], None), (report, [], FACT_CLAIM + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass1={FACT_CLAIM: ("fact_about_existing_code", 0.0)}, pass2={FACT_CLAIM: (0.9, 0.05, 0.05, 0.9)}))
        self.assertIn("[RELAYED] " + FACT_CLAIM, r["reason"])

    def test_same_claim_backed_by_a_read_passes(self):
        turns = [("look", [("grep retries scheduler.rs", "scheduler.rs:40: retries failed jobs three times before giving up")], FACT_CLAIM + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass1={FACT_CLAIM: ("fact_about_existing_code", 0.0)}, pass2={FACT_CLAIM: (0.9, 0.05, 0.05, 0.9)}))
        self.assertIsNone(r)

    def test_subagent_report_does_not_count_as_a_user_turn(self):
        report = "Another Claude session sent a message:\n<agent-message from=\"abc123\">\n[Subagent hand-back] The report follows:\n  hello\n</agent-message>"
        path = transcript([("look", [("ls", "a.txt")], None), (report, [], None), ("next", [], "Done. " + PADDING)])
        _, lines, ages, _ = s.last_turn(path)
        self.assertEqual([a for l, a in zip(lines, ages) if l.startswith("[ls]")], [1])  # one user turn later, not two
        self.assertTrue(any(l.startswith("[agent:abc123]") for l in lines))

    def test_number_absent_from_all_evidence_is_unsupported(self):
        c = "The full workspace run finished with 575 passed and 0 failed across every crate."
        turns = [("run", [("cargo test -p lumen", "test result: ok. 17 passed; 0 failed")], c + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass1={c: ("fact_about_existing_code", 0.0)}, pass2={c: (0.1, 0.05, 0.85, 0.9)}))
        self.assertIn("[UNSUPPORTED] " + c, r["reason"])

    def test_number_present_in_evidence_or_user_prompt_is_fine(self):
        c = "The full workspace run finished with 1 738 passed and 0 failed across every crate."
        turns = [("we had 1738 tests last time", [("cargo test", "test result: ok. 17 passed; 0 failed")], c + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass1={c: ("fact_about_existing_code", 0.0)}, pass2={c: (0.1, 0.05, 0.85, 0.9)}))
        self.assertIsNone(r)

    def test_number_rule_does_not_disable_contradicted(self):
        c = "The server binds to every interface on port 8787 by default."
        turns = [("check", [("grep HOST laya_server.py", "HOST = 127.0.0.1  # server binds port")], c + " " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass2={c: (0.05, 0.9, 0.05, 0.9)}))
        self.assertIn("[CONTRADICTED] " + c, r["reason"])

    def test_claim_that_admits_memory_blocks(self):
        c = "По памяти, планировщик повторяет упавшие задания три раза перед тем, как сдаться."
        ok = "Не по памяти: планировщик повторяет упавшие задания три раза, сверено с scheduler.rs."
        turns = [("look", [("grep retries scheduler.rs", "scheduler.rs:40: retries failed jobs three times")], f"{c} {ok} " + PADDING)]
        r = run_hook(transcript(turns), fake_judge(pass1={c: ("fact_about_existing_code", 0.0), ok: ("fact_about_existing_code", 0.0)},
                                                   pass2={c: (0.9, 0.05, 0.05, 0.9), ok: (0.9, 0.05, 0.05, 0.9)}))
        self.assertIn("[UNVERIFIED] " + c, r["reason"]); self.assertNotIn(ok, r["reason"])

    def test_fenced_lines_with_digits_become_sentences(self):
        got = s.sentences("Result:\n```\ntest result: ok. 575 passed; 0 failed; finished in 2.1s\nrunning 3 tests\n$ cargo test\n```\nAll good and the build is clean now.")
        self.assertTrue(any("575 passed" in x for x in got)); self.assertFalse(any("cargo test" in x for x in got))

    def test_numbers_normalise(self):
        self.assertEqual(s.numbers("1 738 tests, 2,5 slots, v4, line 224"), {"1738", "2.5", "224"})


class Batching(unittest.TestCase):
    def test_pass2_is_chunked_for_batched_backends(self):
        answer = " ".join(f"Claim number {i} says the module handles case {i} correctly today." for i in range(s.CHUNK + 5))
        turns = [("go", [("ls", "README.md")], answer)]
        inner = fake_judge(); calls = []
        def judge(state, q):
            calls.append(len(q)); return inner(state, q)
        with mock.patch.object(s, "PER_CLAIM", set()):
            run_hook(transcript(turns), judge)
        pass2 = [n for n in calls[2:]]  # calls 0 and 1 are pass-1 choice + mutable noul
        self.assertEqual(len(pass2), 2); self.assertEqual(pass2[0], s.CHUNK); self.assertEqual(sum(pass2), s.CHUNK + 5)


class Retrieval(unittest.TestCase):
    def test_excerpt_reports_freshest_line_age(self):
        lines = ["[a] alpha beta", "[b] alpha gamma", "[c] delta"]
        kws = [s.keywords(l) for l in lines]
        idf = {w: 1.0 for k in kws for w in k}
        out, cov, fresh = s.excerpt("alpha beta", lines, kws, idf, ages=[5, 2, 0])
        self.assertEqual(fresh, 2)          # the freshest line that actually matched, not the freshest overall
        self.assertGreater(cov, 0)
        out, cov, fresh = s.excerpt("zeta", lines, kws, idf, ages=[5, 2, 0])
        self.assertEqual((out, cov, fresh), ([], 0.0, None))

    def test_keywords_cover_snake_case_and_cyrillic(self):
        self.assertEqual(s.keywords("LAYA_FIRM"), {"laya", "firm"})
        self.assertIn("проверить", s.keywords("надо проверить статус"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
