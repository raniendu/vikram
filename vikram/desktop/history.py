"""Shared canonical stored conversation projection for desktop inference."""

from .store import Store


def inference_messages(
    store: Store, chat_id: str, message_id: str, vision: bool
) -> tuple[list, list]:
    from vikram.session import stored_context

    return stored_context(store, chat_id, message_id, vision)
