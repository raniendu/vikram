"""The single Vikram agent, backed by local Ollama."""

import pydantic_ai
from pydantic_ai import Agent
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider

pydantic_ai.BANNER_ENABLED = False

DEFAULT_MODEL = "gemma4:26b-a4b-it-qat"
SYSTEM_PROMPT = (
    "You are Vikram, a helpful local assistant. "
    "Answer clearly and directly. Be honest about uncertainty. "
    "You can converse, but you have no tools or access to files or the internet."
)


def build_agent(model: str = DEFAULT_MODEL) -> Agent[None, str]:
    """Build the repository's agent using a model installed in local Ollama."""
    model = model.strip()
    if not model:
        raise ValueError("An Ollama model name is required.")
    return Agent(
        OllamaModel(
            model,
            provider=OllamaProvider(
                base_url="http://localhost:11434/v1", api_key="ollama"
            ),
        ),
        name="Vikram",
        instructions=SYSTEM_PROMPT,
    )
