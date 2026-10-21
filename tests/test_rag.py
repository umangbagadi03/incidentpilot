import unittest

from incidentpilot.config import Settings
from incidentpilot.rag import (HybridRetriever, build_retriever, incident_to_document, load_documents,
                               reciprocal_rank_fusion, save_postmortem, tokenize)
from tests.helpers import FakeEmbedder, FakeReranker, make_settings


def ids_of(results):
    ids = []
    for result in results:
        ids.append(result["id"])
    return ids


class TestHelpers(unittest.TestCase):
    def test_tokenize_drops_short_words_and_stopwords(self):
        self.assertEqual(tokenize("The pod is in a CrashLoopBackOff"), ["pod", "crashloopbackoff"])

    def test_rrf_rewards_documents_that_rank_well_in_both_lists(self):
        bm25 = [("a", 9.0), ("b", 5.0), ("c", 1.0)]
        dense = [("b", 0.9), ("c", 0.8), ("a", 0.1)]
        fused = reciprocal_rank_fusion([bm25, dense])
        self.assertEqual(fused[0][0], "b")       # 2nd in one list, 1st in the other

    def test_rrf_handles_empty_lists(self):
        self.assertEqual(reciprocal_rank_fusion([[], []]), [])


class TestBm25Only(unittest.TestCase):
    def setUp(self):
        settings = Settings()
        self.retriever = HybridRetriever(load_documents(settings.knowledge_dir))

    def test_describe(self):
        self.assertEqual(self.retriever.describe(), "bm25 only")

    def test_deploy_query_finds_deploy_runbook_first(self):
        results = self.retriever.search("rollback previous release version after bad deploy")
        self.assertEqual(results[0]["id"], "bad-deployment")

    def test_bm25_needs_the_same_words(self):
        # Keyword search has no idea that "errors" and "error" are related. This is why we add dense search.
        found = ids_of(self.retriever.search("errors after new release deploy, need rollback", top_k=3))
        self.assertIn("bad-deployment", found)

    def test_memory_query_finds_memory_documents(self):
        found = ids_of(self.retriever.search("memory keeps growing, containers OOMKilled"))
        self.assertIn("memory-leak", found)
        self.assertIn("INC-103", found)

    def test_unrelated_query_returns_nothing(self):
        self.assertEqual(self.retriever.search("banana smoothie recipe"), [])

    def test_top_k_limits_the_results(self):
        self.assertLessEqual(len(self.retriever.search("service error", top_k=2)), 2)

    def test_empty_knowledge_base_does_not_crash(self):
        self.assertEqual(HybridRetriever([]).search("anything"), [])

    def test_added_document_can_be_found(self):
        document = {"id": "x1", "type": "past_incident", "title": "X1: zebra outage", "text": "zebra cache failed"}
        self.retriever.add_document(document)
        self.assertEqual(self.retriever.search("zebra")[0]["id"], "x1")


class TestHybrid(unittest.TestCase):
    """Uses the real Qdrant client (in memory) with fake embeddings, so no model download is needed."""

    def setUp(self):
        settings = Settings()
        self.documents = load_documents(settings.knowledge_dir)
        self.retriever = HybridRetriever(self.documents, embedder=FakeEmbedder())

    def test_describe(self):
        self.assertEqual(self.retriever.describe(), "hybrid (bm25 + dense + rrf)")

    def test_finds_the_right_document(self):
        results = self.retriever.search("errors after new release deploy, need rollback")
        self.assertIn("bad-deployment", ids_of(results))

    def test_dense_search_works_even_when_bm25_finds_nothing(self):
        # No word overlap with any document, so only the dense index can answer. It always returns the closest vectors.
        self.assertGreater(len(self.retriever.search("zzzz qqqq")), 0)

    def test_reranker_changes_the_order(self):
        retriever = HybridRetriever(self.documents, embedder=FakeEmbedder(), reranker=FakeReranker())
        self.assertEqual(retriever.describe(), "hybrid (bm25 + dense + rrf + rerank)")
        results = retriever.search("memory leak OOM restart heap")
        self.assertIn(results[0]["id"], ["memory-leak", "INC-103"])

    def test_added_document_is_searchable_in_both_indexes(self):
        document = {"id": "x2", "type": "past_incident", "title": "X2: kraken outage", "text": "kraken queue stuck"}
        self.retriever.add_document(document)
        self.assertEqual(self.retriever.search("kraken queue")[0]["id"], "x2")


class TestStaleQdrantPoints(unittest.TestCase):
    """A remote Qdrant keeps points between runs. Old points must never break a search."""

    def ghost_point(self, retriever):
        from incidentpilot.rag import COLLECTION
        ghost = {"id": "ghost-doc", "type": "runbook", "title": "Ghost", "text": "kraken queue stuck"}
        retriever.dense.client.upsert(collection_name=COLLECTION, points=retriever.dense.make_points([ghost]))

    def test_unknown_point_is_ignored_not_a_crash(self):
        retriever = HybridRetriever(load_documents(Settings().knowledge_dir), embedder=FakeEmbedder())
        self.ghost_point(retriever)
        results = retriever.search("kraken queue stuck", top_k=10)
        self.assertNotIn("ghost-doc", ids_of(results))

    def test_rebuild_drops_points_from_an_older_run(self):
        from incidentpilot.rag import COLLECTION
        retriever = HybridRetriever(load_documents(Settings().knowledge_dir), embedder=FakeEmbedder())
        self.ghost_point(retriever)
        retriever.dense.build(load_documents(Settings().knowledge_dir))      # the next start-up
        count = retriever.dense.client.count(collection_name=COLLECTION).count
        self.assertEqual(count, len(retriever.documents))


class TestLearning(unittest.TestCase):
    def test_postmortem_round_trip(self):
        settings = make_settings()
        incident = {
            "id": "learned-abc", "title": "Cart outage", "service": "cart", "category": "bad_deploy",
            "symptoms": "carts failed", "root_cause": "bad config", "resolution": "rollback",
        }
        save_postmortem(settings.learned_dir, incident)
        documents = load_documents(settings.knowledge_dir, settings.learned_dir)
        self.assertIn("learned-abc", ids_of(documents))
        self.assertEqual(incident_to_document(incident)["type"], "past_incident")

    def test_build_retriever_without_dense_is_bm25_only(self):
        settings = make_settings()
        settings.use_dense = False
        retriever = build_retriever(settings, load_documents(settings.knowledge_dir))
        self.assertEqual(retriever.describe(), "bm25 only")


if __name__ == "__main__":
    unittest.main()
