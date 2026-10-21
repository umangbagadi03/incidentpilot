"""Optional Langfuse tracing for the LangGraph run (every node shows up in a trace).

It only turns on when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set.
If anything goes wrong, tracing is skipped and the agent still works.
"""
import logging

log = logging.getLogger("incidentpilot.tracing")


def get_callbacks(settings):
    """Return the LangChain-style callbacks to pass to graph.invoke (an empty list means no tracing)."""
    if settings.langfuse_public_key == "" or settings.langfuse_secret_key == "":
        return []
    try:
        try:
            from langfuse.langchain import CallbackHandler      # Langfuse v3
        except ImportError:
            from langfuse.callback import CallbackHandler       # Langfuse v2
        return [CallbackHandler()]                              # reads the keys from the environment
    except Exception as error:
        log.warning("Langfuse tracing is off: %s", error)
        return []
