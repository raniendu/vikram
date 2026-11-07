"""Explicit provider selection, metadata, and private credential storage."""

import ctypes
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol

from vikram.ollama import ThinkingValue, thinking_request

Provider = Literal["ollama", "openrouter"]
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_KEY = "OPENROUTER_API_KEY"
CLOUD_DISCLOSURE = (
    "OpenRouter sends your active conversation history and attachments to "
    "OpenRouter and the selected model's provider."
)


@dataclass(frozen=True)
class ModelRef:
    provider: Provider
    model_id: str

    @classmethod
    def parse(cls, value: str) -> "ModelRef":
        if not isinstance(value, str):
            raise ValueError("A model name is required.")
        value = value.strip()
        provider, separator, model_id = value.partition(":")
        if not separator or provider not in ("ollama", "openrouter"):
            provider, model_id = "ollama", value
        if not model_id or any(c.isspace() or ord(c) < 32 for c in model_id):
            raise ValueError("A model name without whitespace is required.")
        if provider == "openrouter" and (
            "/" not in model_id or any(not part for part in model_id.split("/"))
        ):
            raise ValueError("OpenRouter model names must include provider/model.")
        return cls(provider, model_id)

    @property
    def qualified_id(self) -> str:
        return f"{self.provider}:{self.model_id}"

    @property
    def is_cloud(self) -> bool:
        return self.provider == "openrouter"


def model_settings(model: str, thinking: ThinkingValue | None) -> dict:
    """Map shared selections to the native adapter without model fallbacks."""
    if ModelRef.parse(model).provider == "ollama":
        return {"extra_body": thinking_request(thinking)}
    settings = {"openrouter_provider": {"allow_fallbacks": False}}
    if thinking is not None:
        settings["openrouter_reasoning"] = (
            {"enabled": thinking}
            if isinstance(thinking, bool)
            else {"effort": thinking}
        )
    return settings


class CredentialError(ValueError):
    """A credential failure whose message is safe for display."""


class KeychainBackend(Protocol):
    def get(self, provider: str) -> str | None: ...

    def set(self, provider: str, value: str) -> None: ...

    def delete(self, provider: str) -> None: ...


class MacOSKeychain:
    """Use Security.framework directly: credentials never enter process arguments."""

    SERVICE = b"dev.raniendu.vikram.providers"
    NOT_FOUND = -25300

    def _libraries(self):
        if sys.platform != "darwin":
            raise CredentialError(
                "Keychain storage requires macOS. Use OPENROUTER_API_KEY instead."
            )
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        ptr, uint, char = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p
        signatures = {
            "SecKeychainFindGenericPassword": [
                ptr,
                uint,
                char,
                uint,
                char,
                ctypes.POINTER(uint),
                ctypes.POINTER(ptr),
                ctypes.POINTER(ptr),
            ],
            "SecKeychainAddGenericPassword": [
                ptr,
                uint,
                char,
                uint,
                char,
                uint,
                ptr,
                ctypes.POINTER(ptr),
            ],
            "SecKeychainItemModifyAttributesAndData": [ptr, ptr, uint, ptr],
            "SecKeychainItemDelete": [ptr],
            "SecKeychainItemFreeContent": [ptr, ptr],
        }
        for name, arguments in signatures.items():
            function = getattr(security, name)
            function.argtypes = arguments
            function.restype = ctypes.c_int32
        core.CFRelease.argtypes = [ptr]
        core.CFRelease.restype = None
        return security, core

    def _find(self, security, provider: str, *, secret: bool = False):
        account = provider.encode("utf-8")
        size, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = security.SecKeychainFindGenericPassword(
            None,
            len(self.SERVICE),
            self.SERVICE,
            len(account),
            account,
            ctypes.byref(size) if secret else None,
            ctypes.byref(data) if secret else None,
            ctypes.byref(item),
        )
        if status not in (0, self.NOT_FOUND):
            raise CredentialError("Could not access the Vikram credential in Keychain.")
        return status, item, data, size.value

    def get(self, provider: str) -> str | None:
        if sys.platform != "darwin":
            return None
        security, core = self._libraries()
        status, item, data, size = self._find(security, provider, secret=True)
        if status == self.NOT_FOUND:
            return None
        try:
            return ctypes.string_at(data, size).decode("utf-8")
        finally:
            security.SecKeychainItemFreeContent(None, data)
            core.CFRelease(item)

    def set(self, provider: str, value: str) -> None:
        security, core = self._libraries()
        status, item, _data, _size = self._find(security, provider)
        encoded, account = value.encode("utf-8"), provider.encode("utf-8")
        if status == self.NOT_FOUND:
            status = security.SecKeychainAddGenericPassword(
                None,
                len(self.SERVICE),
                self.SERVICE,
                len(account),
                account,
                len(encoded),
                encoded,
                None,
            )
        else:
            try:
                status = security.SecKeychainItemModifyAttributesAndData(
                    item, None, len(encoded), encoded
                )
            finally:
                core.CFRelease(item)
        if status:
            raise CredentialError("Could not save the Vikram credential in Keychain.")

    def delete(self, provider: str) -> None:
        if sys.platform != "darwin":
            return
        security, core = self._libraries()
        status, item, _data, _size = self._find(security, provider)
        if status == self.NOT_FOUND:
            return
        try:
            status = security.SecKeychainItemDelete(item)
        finally:
            core.CFRelease(item)
        if status:
            raise CredentialError(
                "Could not remove the Vikram credential from Keychain."
            )


class CredentialStore:
    """Environment keys override Keychain; neither source enables a provider."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        keychain: KeychainBackend | None = None,
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self.keychain = keychain if keychain is not None else MacOSKeychain()

    @staticmethod
    def _provider(provider: str) -> None:
        if provider != "openrouter":
            raise CredentialError("Only OpenRouter uses a provider credential.")

    @staticmethod
    def _validate(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise CredentialError("Enter a non-empty API key.")
        value = value.strip()
        if len(value) > 8192 or any(c.isspace() or ord(c) < 32 for c in value):
            raise CredentialError("The API key contains invalid characters.")
        return value

    def get(self, provider: str) -> str | None:
        self._provider(provider)
        value = self.environ.get(OPENROUTER_KEY)
        if value and value.strip():
            return self._validate(value)
        try:
            value = self.keychain.get(provider)
            return self._validate(value) if value is not None else None
        except Exception:
            raise CredentialError(
                "Could not read the OpenRouter key from Keychain."
            ) from None

    def source(self, provider: str) -> str | None:
        self._provider(provider)
        if self.environ.get(OPENROUTER_KEY, "").strip():
            return "environment"
        return "keychain" if self.get(provider) is not None else None

    def set(self, provider: str, value: str) -> None:
        self._provider(provider)
        value = self._validate(value)
        try:
            self.keychain.set(provider, value)
        except Exception:
            raise CredentialError(
                "Could not save the OpenRouter key in Keychain."
            ) from None

    def delete(self, provider: str) -> None:
        """Remove the stored key; an environment override is owned by the caller."""
        self._provider(provider)
        try:
            self.keychain.delete(provider)
        except Exception:
            raise CredentialError(
                "Could not remove the OpenRouter key from Keychain."
            ) from None

    def child_environment(self) -> dict[str, str]:
        """A copy suitable for child processes, excluding provider credentials."""
        return {k: v for k, v in self.environ.items() if k != OPENROUTER_KEY}


def _positive_integer(value) -> int | None:
    return value if type(value) is int and value > 0 else None


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _strings(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def parse_openrouter_model(entry: dict, *, authenticated: bool = False) -> dict:
    """Normalize public metadata without inferring capabilities from model names.

    OpenRouter documents per-model reasoning.supported_efforts and mandatory at
    https://openrouter.ai/docs/guides/best-practices/reasoning-tokens.
    """
    ref = ModelRef.parse(f"openrouter:{entry.get('id', '')}")
    architecture = _object(entry.get("architecture"))
    inputs = _strings(architecture.get("input_modalities"))
    outputs = _strings(architecture.get("output_modalities"))
    parameters = _strings(entry.get("supported_parameters"))
    chat = "text" in inputs and "text" in outputs
    reasoning = _object(entry.get("reasoning"))
    can_think = bool(reasoning) or "reasoning" in parameters
    values: list[ThinkingValue] = []
    if can_think:
        efforts = _strings(reasoning.get("supported_efforts"))
        if "supported_efforts" in reasoning and reasoning["supported_efforts"] is None:
            efforts = ["max", "xhigh", "high", "medium", "low", "minimal", "none"]
        if reasoning.get("mandatory") is not True and (
            reasoning.get("mandatory") is False or "none" in efforts
        ):
            values.append(False)
        values.extend(e for e in efforts if e != "none" and e.strip())
        if not efforts:
            values.append(True)
    default = reasoning.get("default_effort")
    if default not in values or not isinstance(default, str):
        default = (
            True
            if True in values
            else next((v for v in values if isinstance(v, str)), None)
        )
    pricing = {}
    for key, value in _object(entry.get("pricing")).items():
        if (
            not isinstance(key, str)
            or not isinstance(value, (str, int, float))
            or isinstance(value, bool)
        ):
            continue
        try:
            number = Decimal(str(value))
            if number.is_finite() and number >= 0:
                pricing[key] = str(value)
        except InvalidOperation:
            continue
    return {
        "name": ref.qualified_id,
        "provider": ref.provider,
        "model_id": ref.model_id,
        "display_name": (
            entry.get("name") if isinstance(entry.get("name"), str) else ref.model_id
        ),
        "available": chat and authenticated,
        "reason": (
            "This model does not advertise text chat support."
            if not chat
            else None if authenticated else "Sign in to OpenRouter to use this model."
        ),
        "vision": chat and "image" in inputs,
        "tools": chat and "tools" in parameters,
        "can_think": chat and can_think,
        "thinking": list(dict.fromkeys(values)) if chat else [],
        "thinking_default": default if chat else None,
        "size": 0,
        "context_length": _positive_integer(entry.get("context_length")),
        "max_output_tokens": _positive_integer(
            _object(entry.get("top_provider")).get("max_completion_tokens")
        ),
        "pricing": pricing,
        "pricing_unit": "USD per token (prompt and completion)",
    }
