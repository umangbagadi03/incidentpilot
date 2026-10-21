"""Small helpers shared by the tests."""
import hashlib
import json
import re
import tempfile

from incidentpilot.config import Settings
from incidentpilot.demo_llm import DemoLLM
from incidentpilot.rag import HybridRetriever, load_documents
from incidentpilot.service import build_service


def make_settings():
    """Settings that write to a fresh temporary folder, so tests never touch each other or your real data."""
    settings = Settings()
    settings.set_data_dir(tempfile.mkdtemp(prefix="ip_test_"))
    settings.max_steps = 6
    settings.max_fix_attempts = 2
    settings.change_freeze = False
    settings.allowed_services = ["checkout", "payments", "inventory", "search", "orders", "notifications"]
    settings.max_replicas = 10
    settings.learn_from_incidents = False
    settings.use_dense = False
    return settings


def make_retriever(settings):
    """BM25 only. No model download, so the tests are fast and work offline."""
    return HybridRetriever(load_documents(settings.knowledge_dir, settings.learned_dir))


def make_service(llm=None, settings=None, retriever=None):
    if llm is None:
        llm = DemoLLM()
    if settings is None:
        settings = make_settings()
    if retriever is None:
        retriever = make_retriever(settings)
    return build_service(settings, llm, retriever=retriever)


class FakeEmbedder:
    """Deterministic fake embeddings (hashed bag of words). Lets us test the Qdrant path without a model."""

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
        vectors = []
        for text in texts:
            vector = [0.0] * 64
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                slot = int(hashlib.md5(word.encode()).hexdigest(), 16) % 64
                vector[slot] = vector[slot] + 1.0
            squares = 0.0
            for value in vector:
                squares = squares + value * value
            total = squares ** 0.5
            if total > 0:
                for index in range(64):
                    vector[index] = vector[index] / total
            vectors.append(vector)
        return vectors


class FakeReranker:
    """Scores a (query, document) pair by how many query words the document contains."""

    def predict(self, pairs):
        scores = []
        for query, document in pairs:
            score = 0
            for word in set(re.findall(r"[a-z0-9]+", query.lower())):
                if word in document.lower():
                    score = score + 1
            scores.append(float(score))
        return scores


class ScriptedLLM:
    """Returns the given replies one by one. Also counts how many times it was called."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = 0

    def complete(self, system, user):
        reply = self.replies[self.calls]
        self.calls = self.calls + 1
        return reply


def approve_yes(approval):
    return {"approved": True, "reviewer": "test", "comment": ""}


def approve_no(approval):
    return {"approved": False, "reviewer": "test", "comment": "not now"}


def make_evidence(evidence_id, ok=True):
    return {
        "id": evidence_id,
        "step": 1,
        "tool": "get_service_status",
        "arguments": {},
        "kind": "REPLAY",
        "ok": ok,
        "summary": "a bad release went out",
        "data": {},
    }


def json_reply(data):
    return json.dumps(data)
