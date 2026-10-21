"""Tests of the claim-level grounding check and the hypothesis board cleaning."""
import unittest

from incidentpilot.agent import IncidentAgent
from incidentpilot.grounding import check_claim, clean_hypotheses, extract_facts, ground_rca
from tests.helpers import ScriptedLLM, json_reply, make_evidence

ALERT = {"service": "checkout", "title": "test alert"}


def evidence_with(evidence_id, summary, data=None):
    evidence = make_evidence(evidence_id)
    evidence["summary"] = summary
    evidence["data"] = data or {}
    return evidence


DEPLOY = evidence_with("E1", "latest deploy: 2.14.0 13 minutes ago, previous version 2.13.4")
LOGS = evidence_with("E2", "20 lines at ERROR+", {"patterns": [{"example": "KeyError 'discount_code' request_id=12"}]})
STATUS = evidence_with("E3", "error_rate=18.41% (SLO error_rate max 2.0%)")


def rca_with_claims(claims, confidence=0.9):
    return {"root_cause": "Release 2.14.0 was deployed.", "category": "bad_deploy", "summary": "", "confidence": confidence,
            "evidence_ids": [], "claims": claims, "inconclusive": False, "missing_evidence": []}


class TestFacts(unittest.TestCase):
    def test_finds_versions_identifiers_and_numbers(self):
        values = []
        for fact in extract_facts("Release 2.14.0 raises KeyError 'discount_code' on 18% of requests"):
            values.append(fact["value"])
        self.assertIn("2.14.0", values)
        self.assertIn("KeyError", values)
        self.assertIn("discount_code", values)
        self.assertIn("18", values)
        self.assertNotIn("14", values)          # a version is not split into numbers

    def test_plain_sentence_has_no_facts(self):
        self.assertEqual(extract_facts("There was no recent deploy."), [])


class TestClaims(unittest.TestCase):
    def setUp(self):
        self.evidence = {"E1": DEPLOY, "E2": LOGS, "E3": STATUS}

    def test_true_claim_is_verified(self):
        claim = check_claim({"text": "Release 2.14.0 was deployed 13 minutes ago.", "evidence_ids": ["E1"]}, self.evidence)
        self.assertTrue(claim["verified"])

    def test_rounded_number_is_accepted(self):
        claim = check_claim({"text": "The error rate is 18% (SLO 2%).", "evidence_ids": ["E3"]}, self.evidence)
        self.assertTrue(claim["verified"])

    def test_invented_number_is_caught(self):
        claim = check_claim({"text": "The error rate is 45%.", "evidence_ids": ["E3"]}, self.evidence)
        self.assertFalse(claim["verified"])
        self.assertEqual(claim["missing_facts"], ["45"])

    def test_wrong_version_is_caught(self):
        claim = check_claim({"text": "Release 2.15.0 broke checkout.", "evidence_ids": ["E1"]}, self.evidence)
        self.assertFalse(claim["verified"])
        self.assertIn("2.15.0", claim["missing_facts"])

    def test_fact_from_evidence_that_is_not_cited_is_caught(self):
        # KeyError is in E2, but the claim cites only E1.
        claim = check_claim({"text": "Checkout raises KeyError.", "evidence_ids": ["E1"]}, self.evidence)
        self.assertFalse(claim["verified"])

    def test_claim_without_hard_facts_needs_its_words_in_the_evidence(self):
        # The loophole: no numbers or names, and the evidence never mentions a pool.
        claim = check_claim({"text": "The database pool is exhausted.", "evidence_ids": ["E1"]}, self.evidence)
        self.assertFalse(claim["verified"])
        self.assertIn("pool", claim["missing_facts"][0])

    def test_plain_claim_backed_by_its_words_is_verified(self):
        claim = check_claim({"text": "A deploy happened minutes ago.", "evidence_ids": ["E1"]}, self.evidence)
        self.assertTrue(claim["verified"])

    def test_claim_with_nothing_to_check_is_not_verified(self):
        claim = check_claim({"text": "It is so.", "evidence_ids": ["E1"]}, self.evidence)
        self.assertFalse(claim["verified"])

    def test_claim_without_valid_citation_is_not_verified(self):
        claim = check_claim({"text": "There was no deploy.", "evidence_ids": ["E9"]}, self.evidence)
        self.assertFalse(claim["verified"])
        self.assertEqual(claim["evidence_ids"], [])


class TestGroundRca(unittest.TestCase):
    def test_confidence_is_capped_by_grounding_score(self):
        rca = rca_with_claims([
            {"text": "Release 2.14.0 was deployed 13 minutes ago.", "evidence_ids": ["E1"]},
            {"text": "The error rate is 45%.", "evidence_ids": ["E3"]},
        ], confidence=0.95)
        checked = ground_rca(rca, [DEPLOY, LOGS, STATUS])
        self.assertEqual(checked["grounding_score"], 0.5)
        self.assertEqual(checked["llm_confidence"], 0.95)
        self.assertEqual(checked["confidence"], 0.5)
        self.assertFalse(checked["inconclusive"])
        self.assertEqual(checked["evidence_ids"], ["E1"])      # only citations of verified claims

    def test_mostly_invented_rca_becomes_inconclusive(self):
        rca = rca_with_claims([
            {"text": "Release 2.14.0 was deployed 13 minutes ago.", "evidence_ids": ["E1"]},
            {"text": "The error rate is 45%.", "evidence_ids": ["E3"]},
            {"text": "Memory is 9000 MB.", "evidence_ids": ["E3"]},
        ])
        checked = ground_rca(rca, [DEPLOY, LOGS, STATUS])
        self.assertTrue(checked["inconclusive"])
        self.assertIn("1 of 3", checked["missing_evidence"][0])

    def test_root_cause_cannot_say_more_than_its_verified_claims(self):
        rca = rca_with_claims([
            {"text": "Release 2.14.0 was deployed 13 minutes ago.", "evidence_ids": ["E1"]},
            {"text": "The error rate is 18.41% (SLO 2%).", "evidence_ids": ["E3"]},
        ])
        rca["root_cause"] = "A memory leak in the payment cache caused the outage."
        checked = ground_rca(rca, [DEPLOY, LOGS, STATUS])
        self.assertEqual(checked["grounding_score"], 1.0)        # every claim passed...
        self.assertFalse(checked["root_cause_verified"])          # ...but the summary invents a cause
        self.assertTrue(checked["inconclusive"])

    def test_rca_without_claims_uses_the_root_cause_as_one_claim(self):
        rca = rca_with_claims([])
        rca["root_cause"] = "Release 2.14.0 was deployed minutes ago."
        rca["evidence_ids"] = ["E1"]
        checked = ground_rca(rca, [DEPLOY])
        self.assertEqual(len(checked["claims"]), 1)
        self.assertTrue(checked["claims"][0]["verified"])

    def test_agent_applies_the_grounding_check(self):
        reply = json_reply(rca_with_claims([{"text": "The error rate is 45%.", "evidence_ids": ["E3"]}]))
        rca = IncidentAgent(ScriptedLLM([reply])).write_rca(ALERT, [STATUS])
        self.assertTrue(rca.inconclusive)
        self.assertEqual(rca.grounding_score, 0.0)


class TestHypothesisBoard(unittest.TestCase):
    def test_supported_without_evidence_goes_back_to_open(self):
        board = clean_hypotheses([{"id": "H1", "statement": "bad deploy", "status": "supported", "evidence_ids": []}],
                                 [DEPLOY])
        self.assertEqual(board[0]["status"], "open")

    def test_unknown_evidence_ids_are_removed(self):
        board = clean_hypotheses([{"id": "H1", "statement": "x", "status": "refuted", "evidence_ids": ["E1", "E7"]}],
                                 [DEPLOY])
        self.assertEqual(board[0]["evidence_ids"], ["E1"])
        self.assertEqual(board[0]["status"], "refuted")

    def test_missing_ids_are_filled_and_board_is_limited(self):
        hypotheses = []
        for number in range(8):
            hypotheses.append({"statement": "idea " + str(number), "status": "open", "evidence_ids": []})
        board = clean_hypotheses(hypotheses, [])
        self.assertEqual(len(board), 5)
        self.assertEqual(board[0]["id"], "H1")


if __name__ == "__main__":
    unittest.main()
