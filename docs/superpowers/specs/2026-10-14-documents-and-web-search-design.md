# Documents and automatic web search

Date: October 14, 2026

Status: approved by the owner on October 14, 2026, with explicit instruction
to build it and preserve CLI/app model-tool parity. Implementation is tracked
in [the build plan](../plans/2026-10-14-documents-and-web-search.md).

This extends the approved [local chat design](2026-10-14-local-chat-design.md).

## 1. Outcome and confirmed decisions

Vikram should answer questions about attached documents and use current web
information when the selected local model decides it needs it. Keep the Tauri
interface, website theme, saved chats and folders, and local Ollama inference.
CLI and Tauri must use the same model/tool interaction behavior.

The owner selected:

- PDF, Word (`.docx`), plain text/code, CSV, and Excel (`.xlsx`) attachments.
- Automatic web search chosen by the model, using Parallel's free Search MCP.
- Native tool calling for compatible models. Other models retain normal chat
  and attachments, with web search visibly unavailable.
- Shared agent instructions, tool definitions, capability checks, execution
  loop, limits, failure handling, and context policy for CLI and Tauri.

On October 14, read-only Ollama metadata inspection found that all eight
installed models advertise `tools`. This is a capability declaration, not a
claim that every model's tool behavior has been tested.

Success means attaching a supported document, inspecting the extracted text,
asking a question, and receiving a local-model answer. A request for current
information can trigger visible search activity and produce an answer with
openable sources. Restarting preserves the files, transcript, and sources.

## 2. Approach

Introduce a shared Python conversation runner around the existing Pydantic AI
agent, consumed by both the CLI and the private desktop sidecar. It owns model
capability discovery, effective run settings, two bounded web tools, and the
complete model/tool loop. The model can answer immediately or call a web tool
and then continue its answer. The Tauri renderer still uses private IPC.

Sharing only `build_agent()` is insufficient: the current CLI uses `agent.run`
and retains raw message history, while the desktop has a separate streaming
loop and reconstructs history. Replace that behavioral divergence with one
runner and one canonical conversation-context policy. With equivalent model,
thinking, and prepared conversation inputs, both entry points must
produce the same model instructions, tool schemas, model settings, and tool
result handling. Model output can remain nondeterministic.

Terminal formatting, native controls, and storage adapters remain specific to
each interface. CLI conversations remain in memory for the process; desktop
conversations remain durable. This requirement does not introduce desktop
folders or persistent sessions into the CLI.

The alternatives discussed were searching before every explicitly enabled
message, and adding a separate local decision pass for models without tool
support. The owner chose native automatic tool calling. It avoids an extra
planning request on every message and makes capability limits explicit.

This release adds document context, not a document indexing or retrieval
system. It does not add OCR, PowerPoint, legacy `.doc`/`.xls`, audio/video,
cloud inference, arbitrary MCP servers, or general computer tools.

## 3. Document attachments

Use the existing paperclip, native file picker, drag and drop, and clipboard
file handling. Keep the existing PNG, JPEG, and WebP image behavior. A message
can combine text, images, and documents, or contain documents alone.

| Format | Content provided to the local model |
| --- | --- |
| PDF | Extractable text with page labels; embedded pictures are not converted into image inputs |
| Word `.docx` | Main-body paragraphs and tables in document order |
| Plain text/code | Decoded text with line labels, presented as content rather than executed |
| CSV/TSV | Rows and columns with row labels, quoted fields parsed correctly |
| Excel `.xlsx` | Sheet names, cell coordinates, and values; use cached formula values where present, otherwise label the formula as unevaluated |

Plain text covers UTF-8 and BOM-marked UTF-16, including Markdown, JSON, YAML,
TOML, XML, HTML, logs, and common source-code formats. Unsupported encodings and
binary files receive a clear error. HTML and source code remain inert text.

PDFs without extractable text explain that OCR is not supported. Mixed PDFs
report pages without extractable text. Word text inside drawings, headers,
footers, and embedded objects is outside the supported main-body extraction.
Spreadsheet formulas, macros, links, and scripts are never executed. Cached
spreadsheet values are not recalculated.

Import into private app-owned storage before enabling Send. Retain the original
bytes and a bounded, versioned extraction so later movement of the source file
does not break the chat. Do not reparse historical attachments on every reply.

Document cards show the filename, type, size, and extraction status. Opening a
card shows the actual extracted text that is available to the model, with page,
sheet, or line labels. Show partial-extraction notices before sending and in
the saved transcript. Documents work with text-only models; only image inputs
require vision support.

### Proposed bounds

- Four attachments total per message, including images; 10 MiB per file.
- Existing image limits remain 24 megapixels and still images only.
- Extract up to 20,000 characters per document, at most 80,000 for four files.
- Inspect at most 100 PDF pages, 10 workbook sheets, and 1,000 rows per sheet
  or CSV, with at most 100 columns per row. Report which limit was reached.
- Bound archive expansion to 50 MiB and 5,000 entries for Office files.
- End a document extraction after 15 seconds. Run it outside the main service
  event loop so chat cancellation and service shutdown remain responsive.
- Reject encrypted, corrupt, unsupported, or empty documents without losing
  the existing draft or other successful imports.

When a limit truncates extraction, mark the document Partial and include that
fact in the context sent to the model. Never imply the entire document was
read. These are ingestion bounds, not a promise that all text fits every
model's context. Preserve the existing finite-context notice and error recovery.

## 4. Shared automatic search behavior and controls

CLI and desktop chats always use automatic web tools, with no Web on/off
control. This applies to interactive and one-shot CLI calls and future replies
in existing desktop chats, including those previously saved with Web Off.
Retain the legacy preference column for data compatibility but do not use it
to select future run settings or expose it as an editable field. Shared model
preparation determines tool availability solely from refreshed capabilities;
neither adapter carries a mutable web mode.

The CLI rejects the removed `--web` flag. Old `/web` commands explain that
tools are automatic, without changing settings or sending a model request.
Expose the same supported thinking choices through a CLI `--thinking` option,
with Model default as the default in both interfaces. Validate these settings
using shared Ollama
metadata and request mapping. Preserve the CLI's existing model-selection
precedence: `--model`, then `OLLAMA_MODEL`, then the repository default.

Expose web tools only when refreshed Ollama metadata advertises tool calling.
If a model lacks tool support, expose no tools and report Search unavailable,
explaining that another model is needed. Use the same local-model eligibility
and tool-capability checks in both interfaces. Never substitute a different
model automatically. The internal Off policy remains for evaluations, which
must never inherit interactive tool availability.

Capture the effective setting with each reply attempt, just like model and
thinking selection. Preserve historical attempt settings. Retry uses the
current model's capabilities and can make fresh searches; it never repeats
silently.

Both interfaces explain: model inference and document extraction happen on the
Mac; web search sends model-generated search queries, search objectives,
requested URLs, and an opaque chat session ID to Parallel.
Original attachment files and the full conversation are not uploaded as a
search payload. Generated queries can contain details from messages or
attachments, so automatic search is not a local-only data mode. Show the actual
queries with the reply. CLI help and session status explain automatic tools
and their network use.

The app footer shows `Local model · Web available` or
`Local model · Web unavailable` from the selected model's capabilities.
CLI status includes the readable model name and explains that supported models
can search automatically. Both retain exact Ollama IDs for model selection and
requests.

## 5. Parallel integration and free-use boundary

Use `https://search.parallel.ai/mcp` through Streamable HTTP, without an
Authorization header, account, or OAuth flow. Parallel currently documents
anonymous free use at lower rate limits. Anonymous search uses its managed
fast mode. There is no numeric free allowance promised by this spec and no
paid fallback. See [Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp)
and [programmatic authentication](https://docs.parallel.ai/integrations/mcp/programmatic-use).

Expose only `web_search` and `web_fetch`, with locally validated arguments.
Search finds sources; fetch reads relevant public URLs when the model needs
more detail or the user supplies a link. Use one stable opaque session ID per
chat. For the CLI, generate it once per interactive session or one-shot run.
Supply that ID from trusted conversation state, never model arguments; omit
the optional model-name analytics field.

Connect lazily when the first tool is invoked, using an established MCP client
library. A greeting or local document answer that needs no search must not
contact Parallel merely because Auto is selected. The shared runtime ignores
unexpected tools and does not accept remote sampling, shell, file-access, or
elicitation requests. Configuration is fixed to this provider in this release.

Proposed request bounds:

- At most three remote tool calls per reply, executed serially.
- At most five model requests per reply, including the final response. On the
  final request, disable tools so a model cannot continue a tool loop.
- At most three queries per search; 200 characters per query and 1,000 for the
  objective. At most three URLs per fetch, 2,048 characters per URL.
- Public HTTP(S) URLs only; reject credentials, localhost, local hostnames,
  and private or reserved literal IP addresses.
- A 30-second timeout per tool call and 60 seconds of total remote tool time
  per reply; cancellation closes the active request.
- At most six sources and 20,000 excerpt characters across a reply. End an MCP
  operation if its response exceeds 1 MiB. Do not persist arbitrary unbounded
  MCP output.

Budget exhaustion removes web tools for the final answer. A rate limit or
connection failure is visible to both the model and the user. Do not repeatedly
retry, rotate session IDs, or route to a paid endpoint. The model may answer
from available context while clearly saying it could not verify current facts.
Source cards identify successful retrieval separately from failed attempts.

## 6. Answers, sources, and cancellation

Emit common events for model output, search, fetch, sources, notices, and
terminal state. The app shows `Searching the web…` and `Reading sources…`, with
the query or requested URL available to inspect. The CLI renders equivalent
tool activity as intentional user-facing terminal output and prints retrieved
sources with the answer. Keep one-shot answer/source output on stdout and
progress on stderr; do not add diagnostic logging of conversation content.

Keep app Stop enabled through model generation, search, and fetching. CLI
Ctrl-C reaches the same runner cancellation path. The inline terminal interface
returns to input after stopping a reply; a one-shot interruption exits with code
130. Both cancel the active model/tool operation, close transports, and reject
late results. One desktop reply continues to occupy the existing generation
slot until all of its transports have closed.

Keep the full model/tool loop active until the final answer. Intermediate model
text before a tool call must not prematurely mark the reply Complete. Only
answer text enters the assistant message. Per the October 15 owner update,
actual model thinking is streamed separately, bounded to 64,000 characters.
Desktop replies persist it behind a collapsed disclosure; terminal chat exposes
it on demand with Ctrl-T or `/trace`, retaining it in memory only. Thinking never
enters subsequent user-turn context, logs, or evaluation artifacts.

Normalize retrieved sources into stable IDs scoped to the attempt, with title,
URL, retrieval time, and bounded excerpts. Ask the model to cite those sources
in its answer. Always show the retrieved source list independently of whether
the model formats its citations correctly. Label it as retrieved sources, not
proof that every statement is verified. Open links only after a user click.

Treat document and web content as untrusted reference material. Tool names,
destinations, limits, and permissions come from application code. Content from
a file or page cannot expand those permissions. Disable rendered raw HTML and
external images as in the current transcript.

Associate search activity and bounded source evidence with the matching
attempt. Persist it in the desktop chat store and retain it in memory for an
interactive CLI session. On future turns, both use the same canonical context
builder for completed answers and their source evidence in a model-independent
text representation. Do not replay historical tool calls or perform searches
on chat open, restart, or history reconstruction. Failed and stopped attempts
remain excluded from future model context. The CLI must not retain a different
hidden context by passing raw `result.all_messages()` to its next request.

## 7. Implementation boundaries and persistence

Keep shared agent instructions, conversation execution/context, model
capabilities, and MCP tools in interface-independent modules under `vikram/`,
outside `vikram/desktop/`. Both entry points pass explicit run options to the
same implementation; neither defines its own tool policy or agent loop.
Capability-aware instructions must accurately describe available tools in
Auto and Off using the same construction in both interfaces.

The desktop runtime owns persistence, IPC, and event routing. The CLI owns
argument parsing, terminal input/output, and in-memory session storage. Both
consume the shared runner's event stream, including its terminal result;
buffering terminal output must not bypass the shared loop. Inject transport
and event consumers for testing rather than branching behavior on client type.

Keep document extraction separate from conversation execution. Extend the
existing store and strict IPC schemas. Use established document parsers; do
not build PDF or Office format parsers from scratch. Verify their packaged
offline behavior.

Migrate SQLite transactionally from version 1. Existing image records remain
valid. Add document kind, extraction metadata/text, per-chat web preference,
and attempt-scoped search records. Do not log prompts, document content, tool
queries, or replies to diagnostics; their intentional storage is the local
chat library. Deleting a chat removes its app-owned attachments and search
records, never original source files.

Extend the Rust file-picker/drop bridge to supported documents without giving
the renderer arbitrary path access. Keep HTTP networking in the shared Python
runtime, invoked by the sidecar for desktop use, and retain the renderer's
restricted content policy.

Evaluations explicitly select web Off and use the same agent with that
configuration. They must not inherit Auto from interactive-client defaults.
Preserve the existing evaluation grading policy and metrics history, and
record actual agent configuration/provenance without relabeling old records.
Do not introduce a second evaluation-only agent or automatic live web tests.

Preserve the 20 GiB internal-disk reserve during import, temporary extraction,
dependency installation, and packaging. Recheck before allocating bounded
temporary data and fail the affected action clearly if the reserve cannot be
maintained.

## 8. Validation and acceptance

Use temporary chat libraries and generated non-private fixtures:

1. Import each supported format through the service; verify text, page/sheet
   labels, tables, formula handling, previews, and attachment-only messages.
2. Cover malformed files, unsupported encodings, encryption, archive expansion,
   extraction timeouts, partial extraction, count/size limits, and low space.
3. Migrate a version-1 library containing images, drafts, and attempts without
   losing content. Verify restart, retry, deletion, and moved-original behavior.
4. Test search Off and unsupported models make zero remote calls. Mock MCP
   handshake/tool results, rate limits, malformed results, budgets, cancellation,
   and a model response that calls a tool after emitting intermediate text.
5. Check source/event routing while switching chats, persisted source recovery,
   native picker/drop/paste, and no late updates after Stop.
6. Add cross-interface contract tests: supply identical model metadata,
   settings, prepared history, scripted model events, and mocked tool responses
   through CLI and desktop adapters. Compare model-facing instructions,
   request settings, tool schemas/calls/results, follow-up context, source
   evidence, limits, errors, and cancellation. Test rejection of removed CLI
   web controls, automatic tools for existing chats, and
   thinking mapping, and prove evaluations explicitly remain web Off.
   Run focused Python, frontend, and native checks plus formatting and CLI help.
7. Make one small live anonymous Parallel search using a public, non-private
   query, then short local-model tool-call smoke checks through both interfaces.
   Record model behavior honestly; metadata alone does not establish reliable
   tool use or identical nondeterministic answers.
8. Package the Apple Silicon app, verify its signature and sidecar startup, and
   inspect the new controls and previews in the native app while preserving
   the 20 GiB reserve.

All automatic tests use mocked web responses and no live model evaluations.
No billing setup, public service deployment, or factory work is part of this
extension.
