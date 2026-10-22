"""Readable model labels matching the desktop formatter; IDs stay unchanged."""

import re

_WORDS = {
    "ai": "AI",
    "awq": "AWQ",
    "chatgpt": "ChatGPT",
    "deepseek": "DeepSeek",
    "ggml": "GGML",
    "gguf": "GGUF",
    "gpt": "GPT",
    "gptq": "GPTQ",
    "it": "IT",
    "mlx": "MLX",
    "oss": "OSS",
    "qat": "QAT",
}


def _format_word(word: str) -> str:
    if word.lower() in _WORDS:
        return _WORDS[word.lower()]
    if re.fullmatch(r"(?:[ae]?\d+(?:\.\d+)?[bm]|(?:i?q|fp?|bf|r)\d[\w.]*)", word, re.I):
        return word.upper()
    return word[:1].upper() + word[1:]


def format_model_name(model_id: str) -> str:
    """Format family, version, and variant for display without changing selection."""
    namespace, separator, local = model_id.rpartition("/")
    family, _, tag = local.partition(":")
    family = re.sub(
        r"([a-z]{2,})(\d+(?:\.\d+)*)(?=$|[-_])", r"\1 \2", family, flags=re.I
    )
    name = " ".join(_format_word(word) for word in re.split(r"[-_\s]+", family))
    variant = " ".join(_format_word(word) for word in tag.split("-"))
    return f"{namespace}{separator}{name}" + (f" · {variant}" if variant else "")
