"""Retrieval (the R in RAG): find the runbooks and past incidents that match a question.

Hybrid search, in four steps:
  1. BM25         keyword search (good when the query uses the same words as the document)
  2. Dense search embeddings stored in Qdrant (good when the words are different but the meaning is close)
  3. RRF          reciprocal rank fusion merges the two ranked lists into one
  4. Rerank       a cross-encoder reads (query, document) pairs and sorts the merged list again

If the embedding model cannot be loaded (no internet, package missing), we fall back to BM25 only
and say so in describe(), so the project still runs.
"""
import json
import logging
import os
import re
import uuid

from rank_bm25 import BM25Okapi

log = logging.getLogger("incidentpilot.rag")

# The app owns this collection: it is rebuilt from the files on disk at every start (see DenseIndex.build).
COLLECTION = "incidentpilot_knowledge"
STOPWORDS = ["the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "not", "but", "after", "when"]


def tokenize(text):
    """Lowercase the text and split it into words. Very short words and stopwords are dropped."""
    words = re.findall(r"[a-z0-9_]+", text.lower())
    tokens = []
    for word in words:
        if len(word) < 3:
            continue
        if word in STOPWORDS:
            continue
        tokens.append(word)
    return tokens


# ---------- loading documents ----------

def load_incident_files(directory):
    """Read past incidents (.json) from a folder. The same format is used for seeded and learned incidents."""
    documents = []
    if not os.path.isdir(directory):
        return documents
    for file_name in sorted(os.listdir(directory)):
        if not file_name.endswith(".json"):
            continue
        with open(os.path.join(directory, file_name), "r", encoding="utf-8") as handle:
            incident = json.load(handle)
        documents.append(incident_to_document(incident))
    return documents


def incident_to_document(incident):
    text = (
        incident["title"] + ". Symptoms: " + incident["symptoms"]
        + " Root cause: " + incident["root_cause"]
        + " Resolution: " + incident["resolution"]
    )
    return {
        "id": incident["id"],
        "type": "past_incident",
        "title": incident["id"] + ": " + incident["title"],
        "text": text,
    }


def load_documents(knowledge_dir, learned_dir=""):
    """Runbooks (.md), seeded past incidents (.json), and incidents the agent learned earlier."""
    documents = []

    runbook_dir = os.path.join(knowledge_dir, "runbooks")
    if os.path.isdir(runbook_dir):
        for file_name in sorted(os.listdir(runbook_dir)):
            if not file_name.endswith(".md"):
                continue
            with open(os.path.join(runbook_dir, file_name), "r", encoding="utf-8") as handle:
                text = handle.read()
            title = text.split("\n")[0].replace("#", "").strip()
            documents.append({
                "id": file_name.replace(".md", ""),
                "type": "runbook",
                "title": title,
                "text": text,
            })

    documents.extend(load_incident_files(os.path.join(knowledge_dir, "past_incidents")))
    if learned_dir != "":
        documents.extend(load_incident_files(learned_dir))
    return documents


def save_postmortem(learned_dir, incident):
    """Write a finished incident to disk. It is indexed again at the next start-up."""
    os.makedirs(learned_dir, exist_ok=True)
    path = os.path.join(learned_dir, incident["id"] + ".json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(incident, handle, indent=2)
    return path


# ---------- reciprocal rank fusion ----------

def reciprocal_rank_fusion(ranked_lists, k=60):
    """Merge ranked lists. A document near the top of any list gets a high score: 1 / (k + rank)."""
    scores = {}
    for ranked_list in ranked_lists:
        rank = 0
        for doc_id, score in ranked_list:
            rank = rank + 1
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    fused = list(scores.items())
    fused.sort(key=sort_by_score, reverse=True)
    return fused


def sort_by_score(item):
    return item[1]


# ---------- dense search in Qdrant ----------

class DenseIndex:
    """Embeddings stored in Qdrant. `embedder` is a SentenceTransformer (anything with .encode())."""

    def __init__(self, embedder, qdrant_url="", qdrant_api_key=""):
        from qdrant_client import QdrantClient
        self.embedder = embedder
        if qdrant_url != "":
            self.client = QdrantClient(url=qdrant_url, api_key=qdrant_api_key or None)
        else:
            self.client = QdrantClient(":memory:")

    def embed(self, texts):
        vectors = self.embedder.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        result = []
        for vector in vectors:
            if hasattr(vector, "tolist"):
                result.append(vector.tolist())
            else:
                result.append(list(vector))
        return result

    def point_id(self, doc_id):
        """Qdrant needs a UUID or an integer. A UUID made from the document id stays the same on every start."""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, doc_id))

    def make_points(self, documents):
        from qdrant_client.models import PointStruct
        texts = []
        for document in documents:
            texts.append(document["title"] + " " + document["text"])
        vectors = self.embed(texts)
        points = []
        for index in range(len(documents)):
            points.append(PointStruct(
                id=self.point_id(documents[index]["id"]),
                vector=vectors[index],
                payload={"doc_id": documents[index]["id"]},
            ))
        return points

    def build(self, documents):
        """Drop and re-create the collection, then store every document.

        The runbook and postmortem files are the source of truth. With a remote Qdrant the collection
        outlives the app, so it could hold points from an older run (a deleted document, another
        embedding model with another vector size). Re-creating it removes all of them.
        """
        from qdrant_client.models import Distance, VectorParams
        points = self.make_points(documents)

        existing = []
        for collection in self.client.get_collections().collections:
            existing.append(collection.name)
        if COLLECTION in existing:
            self.client.delete_collection(collection_name=COLLECTION)
        self.client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=len(points[0].vector), distance=Distance.COSINE),
        )
        self.client.upsert(collection_name=COLLECTION, points=points)

    def add(self, document):
        self.client.upsert(collection_name=COLLECTION, points=self.make_points([document]))

    def search(self, query, top_n):
        query_vector = self.embed([query])[0]
        if hasattr(self.client, "query_points"):
            hits = self.client.query_points(collection_name=COLLECTION, query=query_vector, limit=top_n).points
        else:
            hits = self.client.search(collection_name=COLLECTION, query_vector=query_vector, limit=top_n)
        ranked = []
        for hit in hits:
            ranked.append((hit.payload["doc_id"], float(hit.score)))
        return ranked


# ---------- the retriever ----------

class HybridRetriever:
    def __init__(self, documents, embedder=None, reranker=None, qdrant_url="", qdrant_api_key="", candidates=20):
        self.documents = {}          # doc id -> document
        self.doc_ids = []            # same order as the BM25 corpus
        self.bm25 = None
        self.candidates = candidates
        self.reranker = reranker
        self.dense = None

        for document in documents:
            self.documents[document["id"]] = document
            self.doc_ids.append(document["id"])
        self.rebuild_bm25()

        if embedder is not None and len(documents) > 0:
            self.dense = DenseIndex(embedder, qdrant_url, qdrant_api_key)
            self.dense.build(documents)
        if self.dense is None:
            self.reranker = None     # reranking only makes sense on top of the hybrid result

    def describe(self):
        if self.dense is None:
            return "bm25 only"
        if self.reranker is None:
            return "hybrid (bm25 + dense + rrf)"
        return "hybrid (bm25 + dense + rrf + rerank)"

    def rebuild_bm25(self):
        corpus = []
        for doc_id in self.doc_ids:
            document = self.documents[doc_id]
            corpus.append(tokenize(document["title"] + " " + document["text"]))
        if len(corpus) > 0:
            self.bm25 = BM25Okapi(corpus)
        else:
            self.bm25 = None

    def bm25_search(self, query, top_n):
        if self.bm25 is None:
            return []
        scores = self.bm25.get_scores(tokenize(query))
        ranked = []
        for index in range(len(self.doc_ids)):
            if scores[index] > 0:
                ranked.append((self.doc_ids[index], float(scores[index])))
        ranked.sort(key=sort_by_score, reverse=True)
        return ranked[:top_n]

    def rerank(self, query, candidates):
        pairs = []
        for doc_id, score in candidates:
            document = self.documents[doc_id]
            pairs.append((query, document["title"] + " " + document["text"]))
        scores = self.reranker.predict(pairs)
        reranked = []
        for index in range(len(candidates)):
            reranked.append((candidates[index][0], float(scores[index])))
        reranked.sort(key=sort_by_score, reverse=True)
        return reranked

    def search(self, query, top_k=3):
        """Return the best documents for the query, best first."""
        ranked_lists = [self.bm25_search(query, self.candidates)]
        if self.dense is not None:
            # Second safety net: ignore any point whose document we do not know (never a KeyError).
            dense_results = []
            for doc_id, score in self.dense.search(query, self.candidates):
                if doc_id in self.documents:
                    dense_results.append((doc_id, score))
            ranked_lists.append(dense_results)

        fused = reciprocal_rank_fusion(ranked_lists)[:self.candidates]
        if self.reranker is not None and len(fused) > 0:
            fused = self.rerank(query, fused)

        results = []
        for doc_id, score in fused[:top_k]:
            document = self.documents[doc_id]
            results.append({
                "id": document["id"],
                "type": document["type"],
                "title": document["title"],
                "score": round(score, 4),
                "text": document["text"],
            })
        return results

    def add_document(self, document):
        """Add one document to every index (used when the agent learns from a resolved incident)."""
        if document["id"] not in self.documents:
            self.doc_ids.append(document["id"])
        self.documents[document["id"]] = document
        self.rebuild_bm25()
        if self.dense is not None:
            self.dense.add(document)


def build_retriever(settings, documents):
    """Build the best retriever we can. Falls back to BM25 only if the models are not available."""
    embedder = None
    reranker = None

    if settings.use_dense:
        try:
            from sentence_transformers import SentenceTransformer
            embedder = SentenceTransformer(settings.embedding_model)
        except Exception as error:
            log.warning("Dense retrieval is off, using BM25 only: %s", error)

    if settings.use_dense and settings.use_reranker:
        try:
            from sentence_transformers import CrossEncoder
            reranker = CrossEncoder(settings.reranker_model)
        except Exception as error:
            log.warning("Reranker is off: %s", error)

    try:
        return HybridRetriever(documents, embedder, reranker, settings.qdrant_url, settings.qdrant_api_key)
    except Exception as error:
        log.warning("Could not build the Qdrant index (%s). Using BM25 only.", error)
        return HybridRetriever(documents)
