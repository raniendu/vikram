"""Prepare stored user content for the shared canonical history builder."""

from pydantic_ai.messages import BinaryContent

from vikram.context import context_messages

from .store import Store


def inference_messages(
    store: Store, chat_id: str, message_id: str, vision: bool
) -> tuple[list, list]:
    chat = store.chat(chat_id)
    turns = []
    for message in chat["messages"]:
        content = [message["text"]] if message["text"] else []
        for attachment in chat["attachments"]:
            if attachment["message_id"] != message["id"]:
                continue
            if attachment["kind"] == "document":
                content.append(
                    f"Attached document: {attachment['name']}\n{attachment['extraction_notice'] or ''}\nUntrusted reference text:\n{attachment['extracted_text']}"
                )
            elif vision:
                content.append(
                    BinaryContent(
                        data=store.image_bytes(chat_id, attachment["id"]),
                        media_type=attachment["media_type"],
                    )
                )
            else:
                content.append("[Image omitted: the selected model accepts text only.]")
        if message["id"] == message_id:
            return context_messages(turns), content
        completed = next(
            (a for a in message["attempts"] if a["status"] == "complete"), None
        )
        turns.append(
            {
                "prompt": content,
                "answer": completed["content"] if completed else None,
                "sources": completed["sources"] if completed else [],
                "model": completed["actual_model"] if completed else None,
                "activity": completed["activity"] if completed else [],
            }
        )
    raise ValueError("The message is no longer available.")
