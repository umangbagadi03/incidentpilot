"""Small wrappers around the LLM providers.

Every wrapper has the same method: complete(system, user) -> text.
The rest of the project only calls this one method, so changing the provider is easy.
Supported: Anthropic, OpenAI, Gemini, and any local model served by Ollama.
"""
import os

from incidentpilot.metrics import LLM_CALLS, LLM_TOKENS


class AnthropicLLM:
    def __init__(self, model):
        import anthropic
        self.client = anthropic.Anthropic(timeout=60.0, max_retries=2)   # reads ANTHROPIC_API_KEY
        self.model = model

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
    def __init__(self, model):
        import openai
        self.client = openai.OpenAI(timeout=60.0, max_retries=2)         # reads OPENAI_API_KEY
        self.model = model

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
    def __init__(self, model):
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "Gemini support requires the google-genai package. Install it with: pip install google-genai"
            ) from exc

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY is required for Gemini.")
        self.client = genai.Client(api_key=api_key)
        self.model = model

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

    if provider == "anthropic":
        model = settings.llm_model
        if model == "":
            model = "claude-sonnet-5-5"
        return AnthropicLLM(model)

    if provider == "openai":
        model = settings.llm_model
        if model == "":
            model = "gpt-4o-mini"
        return OpenAILLM(model)

    if provider == "gemini":
        model = settings.llm_model
        if model == "":
            model = "gemini-2.0-flash"
        return GeminiLLM(model)

    if provider == "ollama":
        model = settings.llm_model
        if model == "":
            model = os.environ.get("OLLAMA_MODEL", "llama3.1")
        return OllamaLLM(model)

    raise RuntimeError("Unknown LLM_PROVIDER: " + settings.llm_provider)
