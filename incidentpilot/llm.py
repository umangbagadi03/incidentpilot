"""Small wrappers around the LLM providers.

Every wrapper has the same method: complete(system, user) -> text.
The rest of the project only calls this one method, so changing the provider is easy.
Supported: Anthropic, OpenAI, Gemini, and any local model served by Ollama.

Two ways to get a model:
  get_llm(settings)                     the server's model, from environment variables
  make_llm(provider, api_key, model)    a model with a key given at runtime ("bring your own key").
                                        The key lives only in this object, in memory.
"""
import os
import re

from incidentpilot.metrics import LLM_CALLS, LLM_TOKENS


DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}


class AnthropicLLM:
    def __init__(self, model, api_key=None):
        import anthropic
        # api_key=None means: read ANTHROPIC_API_KEY from the environment.
        self.client = anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=2)
        self.model = model
        self.api_key = api_key or ""
        self.label = "Claude · " + model

    def complete(self, system, user):
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1500,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception:
            LLM_CALLS.labels(outcome="error").inc()
            raise
        LLM_CALLS.labels(outcome="ok").inc()
        LLM_TOKENS.labels(direction="input").inc(response.usage.input_tokens)
        LLM_TOKENS.labels(direction="output").inc(response.usage.output_tokens)

        text = ""
        for block in response.content:
            if block.type == "text":
                text = text + block.text
        return text


class OpenAILLM:
    def __init__(self, model, api_key=None):
        import openai
        # api_key=None means: read OPENAI_API_KEY from the environment.
        self.client = openai.OpenAI(api_key=api_key, timeout=60.0, max_retries=2)
        self.model = model
        self.api_key = api_key or ""
        self.label = "OpenAI · " + model

    def complete(self, system, user):
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception:
            LLM_CALLS.labels(outcome="error").inc()
            raise
        LLM_CALLS.labels(outcome="ok").inc()
        if response.usage:
            LLM_TOKENS.labels(direction="input").inc(response.usage.prompt_tokens)
            LLM_TOKENS.labels(direction="output").inc(response.usage.completion_tokens)

        content = response.choices[0].message.content
        if content is None:
            return ""
        return content


class GeminiLLM:
    def __init__(self, model, api_key=None):
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "Gemini support requires the google-genai package. Install it with: pip install google-genai"
            ) from exc

        if not api_key:
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY is required for Gemini.")
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.api_key = api_key
        self.label = "Gemini · " + model

    def complete(self, system, user):
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=user,
                config={"system_instruction": system},
            )
        except Exception:
            LLM_CALLS.labels(outcome="error").inc()
            raise
        LLM_CALLS.labels(outcome="ok").inc()

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            input_tokens = getattr(usage, "prompt_token_count", 0) or getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "candidates_token_count", 0) or getattr(usage, "output_tokens", 0) or 0
            LLM_TOKENS.labels(direction="input").inc(input_tokens)
            LLM_TOKENS.labels(direction="output").inc(output_tokens)

        text = getattr(response, "text", "")
        if text is None:
            return ""
        return text


class OllamaLLM:
    def __init__(self, model):
        import openai
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        self.client = openai.OpenAI(base_url=base_url, api_key=os.environ.get("OLLAMA_API_KEY", "ollama"))
        self.model = model
        self.api_key = ""
        self.label = "Ollama · " + model

    def complete(self, system, user):
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception:
            LLM_CALLS.labels(outcome="error").inc()
            raise
        LLM_CALLS.labels(outcome="ok").inc()
        if response.usage:
            LLM_TOKENS.labels(direction="input").inc(response.usage.prompt_tokens)
            LLM_TOKENS.labels(direction="output").inc(response.usage.completion_tokens)

        content = response.choices[0].message.content
        if content is None:
            return ""
        return content


def get_llm(settings):
    """Pick the provider from LLM_PROVIDER, or from whichever API key is set."""
    provider = settings.llm_provider.lower()
    if provider == "google":
        provider = "gemini"

    if provider == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            provider = "gemini"
        elif os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_MODEL"):
            provider = "ollama"
        else:
            raise RuntimeError(
                "No API configuration found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, or OLLAMA_BASE_URL in .env, "
                "or use --demo to run with the scripted demo model."
            )

    if provider in DEFAULT_MODELS:
        return make_llm(provider, None, settings.llm_model)

    if provider == "ollama":
        model = settings.llm_model
        if model == "":
            model = os.environ.get("OLLAMA_MODEL", "llama3.1")
        return OllamaLLM(model)

    raise RuntimeError("Unknown LLM_PROVIDER: " + settings.llm_provider)


def make_llm(provider, api_key, model=""):
    """Build a model for one provider. api_key=None reads the key from the environment."""
    if provider not in DEFAULT_MODELS:
        raise ValueError("unknown provider: " + str(provider) + " (use anthropic, openai or gemini)")
    if model == "":
        model = DEFAULT_MODELS[provider]
    if provider == "anthropic":
        return AnthropicLLM(model, api_key)
    if provider == "openai":
        return OpenAILLM(model, api_key)
    return GeminiLLM(model, api_key)


class FallbackLLM:
    """The server's real model, with the scripted model as a safety net.

    If the real model fails on the FIRST call of an incident (free quota used up, network down), the
    whole incident switches to the scripted model, so a visitor never sees a broken page. A failure in
    the middle of an incident is raised as usual: the incident gets status "error" and can be retried.
    The switch is per incident, so the service makes one FallbackLLM per incident with fresh().
    """

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.calls = 0
        self.using_fallback = False
        self.api_key = getattr(primary, "api_key", "")
        self.label = describe_llm(primary)

    def fresh(self):
        return FallbackLLM(self.primary, self.fallback)

    def complete(self, system, user):
        if self.using_fallback:
            return self.fallback.complete(system, user)
        try:
            text = self.primary.complete(system, user)
        except Exception:
            if self.calls > 0:
                raise
            self.using_fallback = True
            self.label = describe_llm(self.fallback) + " (live model unavailable)"
            return self.fallback.complete(system, user)
        self.calls = self.calls + 1
        return text


def describe_llm(llm):
    """A short name for the UI, for example "Claude · claude-sonnet-5-5" or "Scripted (offline)"."""
    return getattr(llm, "label", type(llm).__name__)


def is_scripted(llm):
    """True if this model never calls a real LLM (the scripted demo model)."""
    return getattr(llm, "scripted", False)


# Key shapes of the supported providers (Anthropic sk-ant-..., OpenAI sk-..., Google AIza...).
# The * also catches masked keys like "sk-proj-****7890" that providers put in their error messages.
KEY_PATTERNS = [r"sk-[A-Za-z0-9_\-*]{8,}", r"AIza[0-9A-Za-z_\-*]{10,}"]


def redact_secrets(text, secrets=None):
    """Remove API keys from a text (an error message can contain part of a key)."""
    text = str(text)
    for secret in secrets or []:
        if secret:
            text = text.replace(secret, "[redacted]")
    for pattern in KEY_PATTERNS:
        text = re.sub(pattern, "[redacted]", text)
    return text
