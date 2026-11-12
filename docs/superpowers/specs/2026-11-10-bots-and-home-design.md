# Bots and an attention-first Home

Date: November 10, 2026

Status: implementation requested by the owner; **partial roadmap implementation**.
This document records both the shipped foundation and the unimplemented items.
It does not authorize VM/network changes, external-job migration, publication,
merge, deployment, or release.

## Outcome and owner decisions

Vikram is a collection of specialist responsibilities with one attention page,
not a list of conversations that happen to have timers. A bot is configured
without chat. The general-purpose default bot remains available for chat, and a
finished specialist run can be discussed in a conversation.

The owner chose:

1. Sandbox-only unattended repository execution, never host commands.
2. Search and page fetching only for unattended monitoring, never the browser.
3. Home and macOS notifications, not Telegram or Pushover.
4. Ollama by default, explicit per-bot OpenRouter opt-in, no automatic fallback.
5. Home first, with default-bot and specialist-run conversations still available.

The existing sandbox has no network, mounted files, Git, or package tools. The
owner did not answer the follow-up request to broaden it; repeated requests to
implement do not grant those capabilities. **Dependency Keeper is blocked.**
Do not implement it using host tools or represent a dependency scan as a verified
update. Local service recovery and host housekeeping have the same boundary.

## Implemented product

- **Bot configuration:** a versioned schema with name, mandate, charter, sources,
  schedule, optional model override, explicit cloud permission, materiality rubric,
  examples and keyword gates, per-run time/request/tool budgets, liveness period,
  and notification policy. JSON export/import is portable; cloud permission is
  stripped and imports start paused. Invalid config is rejected before saving.
- **Templates:** Topic Watcher, Page Watcher, and Release Watcher; unavailable
  Dependency Keeper explains exactly what is missing.
- **Shared runner:** bot tasks use the same SQLite file, next-due file,
  LaunchAgent, runner lock, catch-up math and cancellation as scheduled prompts.
  Runs are fresh unattended sessions, with search/fetch and the offline Python
  sandbox, never host integrations or a browser. They inherit the shared user
  model only; an inherited cloud model without the bot's grant blocks the run.
- **Outcome contract:** process status and product outcome are separate. A typed
  terminal report is required; its outcome is not simply trusted. Successful
  web-tool evidence and fetching each configured URL are required before a run
  can claim quiet. Tool failure or incomplete coverage means blocked; unsupported
  citations are held. These checks verify retrieval, **not semantic truth**.
- **Findings:** typed kind, severity, stable key, normalized-content fingerprint,
  sources, evidence link, timestamps, repeat count, and new/seen/snoozed/resolved/
  dismissed states. Unchanged open findings merge silently. Dismissed/snoozed
  findings stay suppressed until content changes; resolved repeats stay quiet
  inside the dedupe window. Only explicit high-confidence findings can surface;
  medium/low/missing confidence or a failed keyword gate means held.
- **Home:** decisions/failures/blockers, material developments, overdue bots,
  then one health line per bot. Low-noise bot notifications contain only name
  and outcome. Pending/sent/uncertain receipts prevent blind notification retries.
- **Feedback:** explicit Material/Not Material adds a bounded rubric example;
  Not Material also dismisses. Feedback changes no permissions.
- **Evidence:** original report, partial answer, sources, searches, activity,
  model and timing remain on the run. The normal latest-50 retention additionally
  retains runs referenced by open findings; pruned closed links are cleared.
- **Recovery:** interrupted and missed runs surface on Home without replay.
  Failed/blocked/incomplete attempts do not advance the last verified baseline.
  Time budgets include model readiness, and request/tool exhaustion ends as
  incomplete. Liveness and snooze expiry refresh even without database writes.
- **Navigation and migration:** Home is the initial native screen; Bots has a
  typed editor, run history, findings and import/export. Discuss This Run copies
  evidence into a new chat. Existing chats remain unchanged. An explicit
  Convert to Bot preserves a scheduled prompt's ID and history, starts it paused,
  and carries no cloud permission. No automatic external migration occurs.
- **CLI/IPC:** Home, bot list/create/show/edit/run/pause/resume/reset/delete,
  export/import, finding state and feedback, and JSON read surfaces share the
  native app's store and runner. Private stdio commands advertise `bots`.

Bot configuration and task metadata are written together. Schema version 2
migrates version 1 transactionally under a write lock; older Vikram versions
refuse the new schema rather than interpreting it incorrectly. Preserve a copy
of the library before a production upgrade; downgrade to version 1 requires
restoring that copy, not manually editing `user_version`.

## Boundaries and known limits

A finding of kind approval is **a proposal, not an executable approval ticket**.
No action executor or automatic publication is wired to it. Do not add an
Approve button that executes model-provided commands.

Shared default **model** inheritance is implemented; shared budget, thinking and
materiality inheritance is not. The current schedules support the established
once/daily/weekly/monthly/interval forms, not arbitrary RRULE/event graphs.

There is **no dollar spending cap**. Per-run request/tool/time limits are not a
monetary guarantee. OpenRouter remains off unless explicitly granted for that
bot, and the editor discloses cloud transmission/charges. No cloud inference is
needed for tests. Usage cost accounting and a reliable priced-request budget
remain work before claiming spending protection.

Page Watcher currently compares evidence through model context and finding
fingerprints, not a durable normalized full-page snapshot. A model can still
miss a material development or reword a fact enough to reopen it. No precision/
recall or production effectiveness claim is made. The existing web-tool budget
(three requests and up to six returned sources) still applies; configure small
source sets. Failure to cover them is blocked, never quiet. Keyword gating is
applied to reported findings; a separate pre-model relevance stage is not built.

Notifications retain the existing `osascript`/Script Editor permission limit.
A sent receipt confirms local dispatch, not human receipt. Home is authoritative.
Only the most recent delivery fingerprint/receipt is retained per bot. There is
no daily digest, quiet-hours dispatcher or external messaging integration.

## Item-by-item roadmap inventory

Original proposed backlog IDs are retained. **Done** means the described
foundation exists, **partial** means the remaining work is named, and **blocked**
means an owner decision or incompatible sandbox capability prevents completion.
Unimplemented work is not silently reclassified as done.

| ID | State | Coverage / remaining work |
| --- | --- | --- |
| A1 | Partial | Versioned config and JSON round-trip; general YAML parser not added. |
| A2 | Partial | Shared model inheritance and overrides; shared budgets/thinking/policy inheritance remains. |
| A3 | Partial | App editor and CLI create/edit work without chat; create-from-conversation drafts remain. |
| A4 | Partial | Explicit cloud grant and fixed safe unattended tools; general configurable capabilities remain. Host integrations are not authorized for these runs. |
| A5 | Partial | Three watcher templates; dependency, health and housekeeping templates need compatible capabilities. |
| B1 | Partial | Manual and existing calendar/interval schedules; arbitrary RRULE, after-bot/file/webhook events remain. |
| B2 | Blocked | Shared runner lock works; repository isolation, package tools and registry access need a separately approved VM design. |
| B3 | Partial | Runtime time/request/tool limits; reporting reserve, per-bot disk/cost accounting and monetary enforcement remain. |
| B4 | Partial | Typed report plus retrieval/coverage contract; repository and action verification contracts remain. |
| B5 | Partial | Missing model/Ollama/tool failures surface; dedicated Background-policy and VM health probes remain. |
| B6 | Done | Expected completion period, overdue state and periodic Home refresh. |
| C1 | Done | Persistent finding lifecycle, repeat keys, suppression, actions limited to record management. |
| C2 | Done | Native Home, attention/material/health sections, errors and liveness. |
| C3 | Partial | Rubric, examples, keyword gates and hold-uncertain; source allowlists, threshold rules, pre-model screening and quiet hours remain. |
| C4 | Not implemented | Cadenced grouping/digests; unchanged findings are already quiet. |
| C5 | Done | Explicit feedback writes a visible rubric example without granting authority. |
| D1 | Partial | Fixed sandbox-only policy refuses unattended actions by construction; configurable autonomous/approval/forbidden classes remain. |
| D2 | Not implemented | Executable approval queue with reviewed artifacts; findings are not executable approvals. |
| D3 | Not implemented | Safe draft-artifact preparation and inspection. |
| D4 | Not implemented | Model-created bot drafts/approval; unattended bots cannot create bots at all. |
| E1 | Blocked | Dependency Keeper cannot update/test in the current VM. No host fallback. |
| E2 | Partial | Topic Watcher, cited findings and conservative retrieval checks; two-stage semantic screening, full baseline snapshots and labelled quality validation remain. |
| E3 | Blocked | Host service recovery is incompatible with sandbox-only unattended tools. |
| E4 | Blocked | Host/library housekeeping needs an explicit bounded non-model API and approval design. |
| E5 | Partial | Page/Release Watcher templates; durable normalized document snapshots and no-net-change detection remain. |
| F1 | Done | Home and macOS, per-policy dispatch; external channels excluded by owner choice. |
| F2 | Partial | Durable pending/sent/uncertain suppression; full per-delivery history and retry UX remain. |
| F3 | Not implemented | Optional daily Home brief. |
| G1 | Partial | Baseline/outcome/delivery state and run context; general model-accessible state tools/snapshots remain. |
| G2 | Partial | Finding fingerprints; normalized source diff/no-net-change helpers remain. |
| G3 | Partial | Confirmed UI baseline reset preserves run evidence; replay/snapshot restoration remains. |
| H1 | Partial | Existing run evidence UI and outcome reason; config snapshots, separate usage/cost ledger and richer timeline remain. |
| H2 | Partial | Health/findings/run history; monthly spending and auto-pause remain. |
| H3 | Partial | Existing 50-run retention plus open-finding evidence; configurable retention policy remains. |
| I1 | Partial | Shared app/CLI Home/bots/findings; exact standalone `vikram run` alias not added (`vikram bots run` works). |
| I2 | Partial | Foreground headless runner usable by a scheduler; external scheduler receipt/trigger API remains. |
| J1 | Not performed | No external jobs changed; side-by-side validation and retirement require explicit operational approval and a compatible Dependency Keeper. |
| J2 | Partial | Home-first UI and preserved chats; explicit scheduled-prompt conversion, not automatic migration. |
| K1 | Not implemented | Labelled materiality evaluations through the explicit local eval harness. |
| K2 | Partial | Offline config/storage/runner/protocol/Swift regression fixtures and focused CI; full real-task contract fixtures remain with the corresponding bot implementations. |
| L1 | Done | Shareable versioned JSON configs; imports strip cloud permission and start paused. |

## Original implementation verification

Offline tests cover schema validation, cloud grants/default inheritance, config
round trips, migration/conversion, reports/outcomes, source-evidence failures,
materiality gates, dedupe/suppression, feedback, snooze, liveness, delivery receipts,
interruption recovery, budgets/cancellation, and private desktop commands. Native
tests cover wire models and the controller lifecycle through a stub service.
Focused Python bot/scheduler/protocol tests and native Swift tests run in CI;
actions are pinned, checkout credentials are not persisted, permissions are read-only.

The final serial full Python run executed **463 tests: 459 passed, three sidecar
startup errors and one terminal transcript-size failure**. All four failures
reproduced unchanged on `origin/main` (`3f0fd85`) in a separate checkout, using the
same Python environment: three dispatch tests time out at 2 seconds; the terminal
test observes 35,531 characters against a 35,000 bound. A run concurrent with the
Swift build also timed out the existing FIFO subprocess test; that timeout did
not reproduce in the final serial full run. No tests were skipped, loosened, or
silenced here. The **34 bot tests and all 36 Swift tests pass**. Focused changed-area
tests are the CI gate; the full-suite baseline remains an explicit verification limit.

Black, isort, Python compilation, CLI startup/help, Swift build/tests and the
workflow's offline zizmor audit were also checked. A short local Ollama
`ornith-1.5:35b` format-only smoke returned a valid quiet report with no findings;
it used no web tools and is not evidence of monitoring accuracy. No cloud-model
calls or live quality evaluations were run.

Fresh Pi Lens probes include code, documentation and CI configuration. Python/
Swift LSP coverage remains incomplete (unavailable/timeout/push-only responses),
so zero returned findings is not a claim of complete type coverage. Auxiliary
findings in the new bot code were fixed: template copies use deepcopy, corrupt
state/source JSON fails visibly, and boolean SQLite bindings need no numeric
parse. Numeric schedule repair now also rejects nonfinite/unparseable inputs.
Use the formatter/syntax/offline Python checks and actual Swift build/tests as
fallback verification. Packaged-install/real-LaunchAgent and visual UI validation
were not performed in this implementation turn.
No installed user library, LaunchAgent, external schedules, cloud-model calls,
release tag, merge, or deployment is part of this implementation turn.

## Follow-up: Bots page interaction correction

The first shipped page rendered template rows as descriptive text, not buttons.
Its editor did not load template-owned defaults: Page Watcher incorrectly began
with the generic weekly schedule instead of its daily 09:00 schedule. Template
loading errors were silently converted to an empty array; detail errors could
leave an indefinite spinner or stale selection. A successful create followed by
a failed refresh was also reported as a failed mutation, inviting duplicate bots.

The correction adds explicit Use Template controls and one seeded editor shared
with New Bot. Read-only defaults come from the existing template definitions,
not duplicated Swift constants. Unavailable templates cannot open a creation
draft or acquire capabilities. New drafts never inherit cloud grants. Edit
preserves monthly/one-time schedules and disabled liveness; explicit null values
survive subsequent updates and config import/export.

List/template/detail failures have visible retry states. Late detail responses
cannot replace a newer selection. An acknowledged mutation remains successful
when a later refresh fails, with a separate stale-status warning. Run navigation
clears old evidence before loading the requested run.

Regression coverage includes each template's seed values, template changes,
form validation, optional-value preservation, template retry, successful-create/
failed-refresh handling, late detail responses, and native-shaped field payloads
through the real private Python protocol. An attempted native click test could
not retrieve SwiftUI controls in this environment; Screen Recording and
Accessibility permissions were denied. It is not counted as passing UI
verification and no existing tests are disabled or weakened.

Focused verification passed: 37 bot tests, 32 scheduler tests, seven private
protocol tests, and all 45 native tests in a serial run, plus the native build,
formatting and syntax checks. A concurrent native test run failed in the unchanged
WebKit page-script suite and aborted on its existing out-of-range assertion; the
serial rerun passed without altering that test. Fresh Pi Lens probes returned no
findings but retained incomplete primary LSP coverage. None of these checks is a
claim that the installed UI was click-tested.

Manual UI acceptance still required on a permission-enabled Mac:

1. Click each supported template row; inspect its name, mandate, charter, rubric,
   schedule and liveness defaults. Cancel and verify no bot was created.
2. Confirm blocked templates show their reason and cannot open an editor.
3. Create a personalized bot, inspect its detail, edit it, navigate back, and
   reopen it. Confirm the intended schedule and optional values survived.
4. Retry a failed list/template/detail load. A failed refresh after creation
   must not invite a second creation or show another bot's detail/evidence.
