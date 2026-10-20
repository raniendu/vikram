# Vikram local chat: discovery

Status: discovery record. The consolidated
[product spec](2026-10-14-local-chat-design.md) is the draft for owner review.

## Intended outcome

Use Vikram as a Tauri desktop app for chatting with locally installed Ollama
models. Choose a model for each conversation, maintain multiple chat sessions,
and organize them into folders. Match the visual theme of
[raniendu.dev](https://raniendu.dev/).

These requirements come from the owner's October 14, 2026 local-chat request.
The discarded software-factory specifications do not define this app.

## Confirmed requirements

- A Tauri user interface.
- Chat with models installed locally in Ollama.
- A model selector for each chat.
- Change models within an existing chat while keeping its conversation history;
  the selected model handles subsequent replies.
- Support text and image attachments in the first version, with image input
  available when the selected model supports images.
- Allow switching an image conversation to a text-only model with a warning;
  retain saved images but send only text to that model.
- Multiple chat sessions, one level of folders, and chats outside folders.
- A visual theme drawn from the owner's website.
- Reuse the existing Python/Pydantic AI agent for desktop chat so its behavior
  is shared with the CLI.
- Initial chat controls are sending, stopping, copying, and retrying failed
  replies. Editing sent messages and regenerating completed replies are deferred.
- A per-chat thinking control shows only settings supported by the selected
  model.
- Work through the specifications before implementation.

## Existing repository

The current implementation is a Python CLI using one Pydantic AI agent and
local Ollama. Interactive chat history currently lasts only for the process.
Separate evaluation tools save metrics and reports locally. There is currently
no desktop application or persistent chat store.

This is an architectural addition: desktop presentation and persistent chat
storage need their own boundaries. The owner chose to reuse the Python agent
for desktop inference.

## Website reference

Observed on October 14, 2026 from the rendered homepage and its computed styles.
Both light and dark palettes were inspected. The table records the light
palette; the consolidated spec includes both palettes.

| Role | Observed value |
| --- | --- |
| Background | `#fbfaf8` |
| Secondary surface | `#f4f2ec` |
| Main text | `#14130f` |
| Muted text | `#5c5952` |
| Secondary muted text | `#6f6b62` |
| Borders | `#e3e0d8` |
| Accent | `oklch(0.52 0.13 47)` |
| Display font | Space Grotesk |
| Body and controls | Inter |
| Monospace | System monospace / SF Mono |
| Corner radius | `3px` |

The visual direction is warm ivory surfaces, near-black text, a restrained
copper accent, fine dividers, modest corners, and generous spacing. Adapt the
typography to a compact desktop interface. The dark palette uses `#14130f` for
the background, `#1c1a16` for surfaces, `#f4f2ec` for text, `#2b2924` for borders,
and `oklch(0.72 0.12 55)` for the accent.

## Proposed defaults to discuss

- Start with macOS, given the owner's existing Mac and Tauri preference.
- Keep conversations, folder membership, and model selections on this Mac
  between launches.
- Connect to the existing local Ollama installation and discover its models.
- Put folders and chats in a sidebar, with the selected conversation and a
  visible model picker in the main area.
- Stream replies with a stop control and render Markdown and code blocks.
- Preserve the existing CLI, evaluations, and saved evaluation history.

These are recommendations, not additional confirmed requirements.

## Decisions

### Model switching

The owner chose to continue the same conversation when changing models.
Preserve the stored history and use the newly selected model for subsequent
replies. Record and display the model that actually produced each reply.
Changing the selection does not rewrite previous replies.

### Message types

The first version supports text and image attachments. Image input depends on
the selected model's image capability. Capability differences must be clear in
the model picker and composer.

The owner chose to allow switching an image conversation to a text-only model.
Warn that the selected model cannot see the images and send it only text.
Keep the images in the stored chat and visible in the transcript. They remain
available when an image-capable model is selected again. Do not silently delete
attachments or pretend that a text-only model received them.

### Folders

The owner chose one level of folders. Chats may belong to a folder or remain
outside folders. Subfolders are outside the initial scope.

### Architecture

Two approaches were presented:

1. Tauri calls Ollama directly: simpler desktop packaging, but desktop chat
   would not run through the existing Python agent.
2. Tauri reuses the Python/Pydantic AI agent: shared behavior with the CLI,
   with an additional Python process to package and manage.

The owner selected the second approach. The proposed implementation is a
Python sidecar managed by the desktop app. It imports the shared agent rather
than defining a second assistant. Chat and image persistence remain local.

References: [Tauri architecture](https://v2.tauri.app/concept/architecture/),
[Tauri sidecars](https://v2.tauri.app/develop/sidecar/), and
[Ollama chat API](https://docs.ollama.com/api/chat).

### Chat controls

The owner selected sending messages, stopping an active reply, copying content,
and retrying failed replies for the first release. Editing sent messages and
regenerating completed replies are outside the initial scope.

### Thinking controls

The owner selected a per-chat thinking control with only the values supported
by the selected model. Discover these from Ollama metadata rather than guessing
from model names.

## Review

The consolidated spec distinguishes the owner's confirmed choices from proposed
defaults for platform, persistence, appearance, concurrency, and routine chat
management. Implementation has not started.
