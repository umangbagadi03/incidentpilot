"""Grounding checks: is what the LLM wrote really backed by the evidence?

Citing "E2" is easy for an LLM, even when E2 does not say what the claim says.
So we check every claim, in plain Python:

  1. The claim must cite at least one real, successful evidence item.
  2. Every hard fact in the claim must appear in the evidence it cites.
     Hard facts are: version numbers (2.14.0), numbers (18%, 2048), code identifiers
     (KeyError, OOMKilled, discount_code) and quoted strings ('discount_code').
  3. At least half of the claim's key words must appear in the cited evidence too.
     Without this, "The database pool is exhausted" (no hard facts at all) would pass
     against evidence that never mentions a pool.
  A claim with nothing to check (no hard facts, no key words) is NOT verified.

A claim that passes all checks is "verified". Then:
  grounding_score = verified claims / all claims
  final confidence = min(LLM confidence, grounding_score)
An RCA with no verified claim, or with less than half of its claims verified, becomes inconclusive.
The root_cause sentence itself is checked the same way against the evidence of the verified claims,
so a summary cannot say more than its claims prove.

Be honest about the limit: this is a LEXICAL check (facts and words present), not a semantic one.
"Memory is NOT the cause" and "memory is the cause" share the same words. Meaning is left to the
hypothesis board, the verification after the fix, and the human approver.

The same file also cleans the hypothesis board: a hypothesis can only be "supported" or "refuted"
if it points to real evidence.
"""
import json
import re

VERSION_PATTERN = r"\bv?\d+\.\d+\.\d+\b"
NUMBER_PATTERN = r"\d+(?:\.\d+)?"
QUOTED_PATTERN = r"'([^']{2,40})'"
# KeyError, OutOfMemoryError, OOMKilled, PoolTimeout (a capital letter inside the word)
CAMEL_PATTERN = r"\b[A-Za-z]*[a-z][A-Z][A-Za-z]*\b|\b[A-Z]{2,}[a-z][A-Za-z]*\b"
# discount_code, SMTP_HOST (a word with an underscore)
SNAKE_PATTERN = r"\b[A-Za-z]+_[A-Za-z_]+\b"

MIN_GROUNDING_SCORE = 0.5
MIN_WORD_SUPPORT = 0.5

# Words that say nothing about the incident, so they are not counted as key words.
STOPWORDS = [
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "not", "but", "after", "when",
    "since", "then", "than", "there", "their", "which", "while", "into", "over", "under", "about", "every",
    "each", "also", "only", "very", "more", "most", "some", "such", "have", "has", "been", "being", "does",
    "did", "done", "will", "would", "could", "should", "because", "they", "them", "these", "those", "what",
    "where", "why", "how", "all", "any", "both", "still", "just", "even", "until", "again", "here", "within",
    "so", "its", "service", "about", "caused", "causes", "explain", "explains", "shows", "show", "says",
]


# ---------- facts ----------

def extract_facts(text):
    """Return the hard facts in a sentence, as {"kind": ..., "value": ...} dicts."""
    facts = []
    seen = []

    def add(kind, value):
        if value.lower() not in seen:
            seen.append(value.lower())
            facts.append({"kind": kind, "value": value})

    for version in re.findall(VERSION_PATTERN, text):
        add("text", version.lstrip("v"))
    for quoted in re.findall(QUOTED_PATTERN, text):
        add("text", quoted)
    for word in re.findall(CAMEL_PATTERN, text):
        add("text", word)
    for word in re.findall(SNAKE_PATTERN, text):
        add("text", word)

    # Numbers: remove versions first, so "2.14.0" is not also read as the numbers 2.14 and 0.
    without_versions = re.sub(VERSION_PATTERN, " ", text)
    for number in re.findall(NUMBER_PATTERN, without_versions):
        add("number", number)
    return facts


def numbers_in(text):
    """Every number in a text, as floats."""
    result = []
    without_versions = re.sub(VERSION_PATTERN, " ", text)
    for number in re.findall(NUMBER_PATTERN, without_versions):
        result.append(float(number))
    return result


def number_is_close(claimed, numbers):
    """18% in a claim matches 18.41 in the evidence (5% tolerance, so rounding is allowed)."""
    value = float(claimed)
    for number in numbers:
        tolerance = max(abs(number) * 0.05, 0.5)
        if abs(number - value) <= tolerance:
            return True
    return False


def evidence_text(evidence):
    """All text of one evidence item: the summary and the full tool output."""
    return evidence["summary"] + " " + json.dumps(evidence["data"])


def content_words(text):
    """The key words of a sentence: lowercase, 4+ letters, not a stopword, no duplicates."""
    words = []
    for word in re.findall(r"[a-z][a-z_]+", text.lower()):
        if len(word) >= 4 and word not in STOPWORDS and word not in words:
            words.append(word)
    return words


def word_in_text(word, text_lower):
    """'exhausted' matches 'exhaustion': compare the first 5 letters (a cheap stand-in for stemming)."""
    if len(word) > 5:
        word = word[:5]
    return word in text_lower


# ---------- claims ----------

def check_claim(claim, evidence_by_id):
    """Fill claim["verified"] and claim["missing_facts"]. `claim` is a dict, changed in place."""
    valid_ids = []
    for evidence_id in claim["evidence_ids"]:
        if evidence_id in evidence_by_id:
            valid_ids.append(evidence_id)
    claim["evidence_ids"] = valid_ids

    if len(valid_ids) == 0:
        claim["verified"] = False
        claim["missing_facts"] = ["no valid evidence cited"]
        return claim

    cited_text = ""
    for evidence_id in valid_ids:
        cited_text = cited_text + " " + evidence_text(evidence_by_id[evidence_id])
    cited_lower = cited_text.lower()
    cited_numbers = numbers_in(cited_text)

    facts = extract_facts(claim["text"])
    missing = []
    for fact in facts:
        if fact["kind"] == "number":
            if not number_is_close(fact["value"], cited_numbers):
                missing.append(fact["value"])
        else:
            if fact["value"].lower() not in cited_lower:
                missing.append(fact["value"])

    # Key words: at least half must be in the cited evidence.
    words = content_words(claim["text"])
    words_found = 0
    words_missing = []
    for word in words:
        if word_in_text(word, cited_lower):
            words_found = words_found + 1
        else:
            words_missing.append(word)
    if len(facts) == 0 and len(words) == 0:
        missing.append("nothing in the claim can be checked")
    elif len(words) > 0 and words_found / len(words) < MIN_WORD_SUPPORT:
        missing.append("words not in evidence: " + ", ".join(words_missing))

    claim["missing_facts"] = missing
    claim["verified"] = len(missing) == 0
    return claim


def ground_rca(rca, evidence_list):
    """Check every claim of an RCA (a dict) against the evidence. Returns the changed RCA dict."""
    evidence_by_id = {}
    for evidence in evidence_list:
        if evidence["ok"]:
            evidence_by_id[evidence["id"]] = evidence

    # An LLM that gave no claims: the root cause itself is the one claim.
    if len(rca["claims"]) == 0:
        rca["claims"] = [{"text": rca["root_cause"], "evidence_ids": list(rca["evidence_ids"])}]

    verified_count = 0
    cited = []
    for claim in rca["claims"]:
        check_claim(claim, evidence_by_id)
        if claim["verified"]:
            verified_count = verified_count + 1
            for evidence_id in claim["evidence_ids"]:
                if evidence_id not in cited:
                    cited.append(evidence_id)

    score = verified_count / len(rca["claims"])
    rca["grounding_score"] = round(score, 2)
    rca["llm_confidence"] = rca["confidence"]
    rca["confidence"] = round(min(rca["confidence"], score), 2)
    rca["evidence_ids"] = sorted(cited, key=evidence_number)

    # The root cause sentence is bound to the verified claims: it may only use what their evidence shows.
    root_claim = check_claim({"text": rca["root_cause"], "evidence_ids": list(rca["evidence_ids"])}, evidence_by_id)
    rca["root_cause_verified"] = root_claim["verified"]
    rca["root_cause_missing"] = root_claim["missing_facts"]

    if not rca["inconclusive"]:
        if verified_count == 0:
            rca["inconclusive"] = True
            rca["missing_evidence"].append("no claim in the RCA is backed by the evidence it cites")
        elif score < MIN_GROUNDING_SCORE:
            rca["inconclusive"] = True
            rca["missing_evidence"].append(
                "only " + str(verified_count) + " of " + str(len(rca["claims"])) + " claims are backed by evidence")
        elif not root_claim["verified"]:
            rca["inconclusive"] = True
            rca["missing_evidence"].append(
                "the root cause says more than the verified claims show (" + "; ".join(root_claim["missing_facts"]) + ")")
    if rca["inconclusive"] and len(rca["missing_evidence"]) == 0:
        rca["missing_evidence"] = ["evidence that clearly shows the root cause"]
    return rca


def evidence_number(evidence_id):
    """'E12' -> 12, so E2 sorts before E10."""
    digits = evidence_id[1:]
    if digits.isdigit():
        return int(digits)
    return 0


# ---------- hypothesis board ----------

def clean_hypotheses(hypotheses, evidence_list, max_items=5):
    """Keep the board honest. `hypotheses` is a list of dicts.

    - unknown or failed evidence ids are removed
    - "supported" or "refuted" without any evidence becomes "open" again
    - missing ids get H1, H2, ...
    """
    real_ids = []
    for evidence in evidence_list:
        if evidence["ok"]:
            real_ids.append(evidence["id"])

    board = []
    for index, hypothesis in enumerate(hypotheses[:max_items]):
        item = dict(hypothesis)
        if item.get("id", "") == "":
            item["id"] = "H" + str(index + 1)
        kept_ids = []
        for evidence_id in item.get("evidence_ids", []):
            if evidence_id in real_ids:
                kept_ids.append(evidence_id)
        item["evidence_ids"] = kept_ids
        if item["status"] != "open" and len(kept_ids) == 0:
            item["status"] = "open"
        board.append(item)
    return board
