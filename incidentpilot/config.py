"""Settings. Everything comes from environment variables (or a .env file)."""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_list(name, default):
    """Read a comma separated environment variable as a list."""
    value = os.environ.get(name, default)
    items = []
    for part in value.split(","):
        part = part.strip()
        if part != "":
            items.append(part)
    return items


def get_bool(name, default):
    value = os.environ.get(name, default).strip().lower()
    return value in ["1", "true", "yes", "on"]


class Settings:
    def __init__(self):
        # --- LLM ("auto" picks Anthropic or OpenAI from the API key you set) ---
        self.llm_provider = os.environ.get("LLM_PROVIDER", "auto")
        self.llm_model = os.environ.get("LLM_MODEL", "")

        # --- Agent limits ---
        self.max_steps = int(os.environ.get("MAX_STEPS", "6"))               # tool calls per investigation
        self.max_fix_attempts = int(os.environ.get("MAX_FIX_ATTEMPTS", "2"))  # fixes tried before giving up

        # --- Safety rules (policy.py) ---
        self.allowed_services = get_list("ALLOWED_SERVICES", "checkout,payments,inventory,search,orders,notifications")
        self.max_replicas = int(os.environ.get("MAX_REPLICAS", "10"))
        self.change_freeze = get_bool("CHANGE_FREEZE", "false")

        # --- Retrieval (rag.py) ---
        self.use_dense = get_bool("USE_DENSE_RETRIEVAL", "true")      # embeddings in Qdrant
        self.use_reranker = get_bool("USE_RERANKER", "true")          # cross-encoder on the fused results
        self.embedding_model = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        self.reranker_model = os.environ.get("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
        self.qdrant_url = os.environ.get("QDRANT_URL", "")            # empty = in-memory Qdrant
        self.qdrant_api_key = os.environ.get("QDRANT_API_KEY", "")
        self.learn_from_incidents = get_bool("LEARN_FROM_INCIDENTS", "true")

        # --- Folders and files ---
        self.scenarios_dir = os.environ.get("SCENARIOS_DIR", os.path.join(ROOT, "scenarios"))
        self.knowledge_dir = os.environ.get("KNOWLEDGE_DIR", os.path.join(ROOT, "knowledge"))
        self.reports_dir = os.environ.get("REPORTS_DIR", os.path.join(ROOT, "reports"))
        self.set_data_dir(os.environ.get("DATA_DIR", os.path.join(ROOT, "data")))

        # --- Optional Langfuse tracing ---
        self.langfuse_public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        self.langfuse_secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")

        # --- Server ---
        self.host = os.environ.get("HOST", "127.0.0.1")
        self.port = int(os.environ.get("PORT", "8000"))

    def set_data_dir(self, data_dir):
        """Everything the app writes lives under one folder."""
        self.data_dir = data_dir
        self.db_path = os.path.join(data_dir, "incidents.db")             # incident list for the API
        self.checkpoint_path = os.path.join(data_dir, "checkpoints.db")   # LangGraph checkpoints
        self.learned_dir = os.path.join(data_dir, "learned")              # postmortems written by the agent

    def make_dirs(self):
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.learned_dir, exist_ok=True)
        os.makedirs(self.reports_dir, exist_ok=True)
