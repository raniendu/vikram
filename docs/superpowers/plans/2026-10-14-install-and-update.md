# Install and Update Implementation Plan

> Execute inline using the executing-plans and verification-before-completion
> skills. Preserve all existing work; no commits, pushes, or publication.

**Goal:** One Mac installer and one CLI command install/update both interfaces.

**Architecture:** Reuse the bundled Python runtime as the installed CLI. A
per-user installer stages complete app versions and atomically switches one
shared pointer. A bounded GitHub client verifies release checksums before
passing the payload to the same installation engine.

**Tech Stack:** Python 3.13, stdlib, httpx, PyInstaller, Tauri 2, AppKit installer.

**Spec:** `docs/superpowers/specs/2026-10-14-install-and-update-design.md`

## Global Constraints

- Apple Silicon, macOS 13+, current account only, no administrator access.
- Preserve at least 20 GiB free; never modify chats, models, or Vikram Studio.
- Keep CLI and desktop on one release; no credentials or inference calls.
- Stable releases only from `raniendu/vikram`; require GitHub SHA-256 digest.
- Preserve local changes; no commits, pushes, releases, or Apple uploads.

## Review Focus

- Interrupted or failed installation must keep the previous version usable.
- Existing uv launcher must remain recoverable; unfamiliar files must survive.
- Archive traversal, symlinks, case collisions and expansion must be rejected.
- Running apps must keep their files until the user reopens them.
- A missing release, unsupported platform or version mismatch must be explicit.

## Task 1: Shared installation engine and bundled CLI

Files: `vikram/installation.py`, `vikram/bundle.py`, `vikram/cli.py`,
`scripts/desktop_sidecar.py`, `tests/test_installation.py`.

Interface: `Layout(home)`, `install_bundle(source: Path, layout: Layout) -> str`;
`installed_version(layout) -> str | None`; `bundle.main() -> int` dispatches
desktop, document worker, CLI, and installer modes. Version comes from installed
Python metadata and must match the app plist.

- [x] Write and run failing tests for first install, shared pointer switching,
  health-check failure, disk reserve, migration, conflicts, and repeat installs.
- [x] Implement managed paths, serialized staging, validation, atomic activation,
  safe shell setup and launcher migration; run targeted tests to green.
- [x] Add `--version` and bundled CLI dispatch without changing chat behavior.

## Task 2: Verified stable release updates

Files: `vikram/releases.py`, `vikram/cli.py`, `tests/test_releases.py`.

Interface: `latest_release(client) -> Release | None`,
`download_release(client, release, destination) -> Path`,
`extract_release(archive, destination) -> Path`, `update_main(argv) -> int`.

- [x] Write and run failing tests for available/current/older/no releases,
  corrupt checksums, unsafe metadata, redirects, extraction, transport failure,
  and the `update` command versus a literal prompt.
- [x] Implement bounded HTTPS retrieval and extraction, then use Task 1 for
  activation. Verify `--check` is read-only and failures preserve current.

## Task 3: Installer artifact and validation

Files: `scripts/build_installer.py`, `scripts/installer.swift`,
`scripts/build_desktop.py`, `README.md`, `AGENTS.md`, release instructions.

- [x] Compile a minimal native installer that invokes the bundled installer
  mode and displays success or a useful error.
- [x] Check all release versions agree; package and verify installer signatures,
  create the versioned ZIP/checksum, and document installation/update/release use.
- [x] Test installation of the real bundle in a disposable account directory,
  run its CLI startup/version, and verify the desktop sidecar handshake.
- [x] Run focused offline tests, CLI startup, formatting, existing frontend and
  native checks as appropriate. Perform a fresh independent review, fix findings,
  and record actual results and distribution limitations here.

## Execution record

- Planning: GitHub repository is public with no releases. Existing CLI is a uv
  tool symlink; no `~/Applications/Vikram.app` exists. About 65 GiB free.
- Ruling: proceed with the requested installer implementation using the defaults
  above. The owner explicitly requested the work; another design-approval pause
  would be redundant. Existing app/model behavior remains the approved baseline.
- Task 1: complete. Installation tests were written before implementation and
  initially failed on the missing module. Core installation/migration/rollback,
  reserve and conflict checks passed. An interrupted uv migration was reproduced
  and fixed so retries reuse the preserved backup.
- Task 2: complete. Release tests initially failed on the missing module.
  Checksummed downloads, safe extraction, current/older/missing releases,
  failed network/download/version checks and shared activation passed. Implicit
  directory case collisions were reproduced with a failing test and rejected.
  The updater holds the installation lock throughout download/extraction and
  rechecks the installed version before downloading.
- Task 3: complete. Built the final local native installer and versioned ZIP on
  Apple Silicon macOS 27.2. Build validates Python/Tauri/frontend/Rust versions,
  app and installer code signatures, ZIP extraction permissions and packaged
  CLI/service startup. The final ZIP passed its SHA-256 check and the disposable
  installation/reinstallation smoke script, including paths containing spaces.
- Final review: fresh-context GPT-6 Astra review found one Important issue and
  no Critical/Minor issues. Cancellation just after the atomic pointer rename
  could delete the active candidate because an in-memory flag lagged the rename.
- Final: fixed cancellation after activation —
  `test_interrupt_after_atomic_switch_keeps_new_release_and_allows_retry`
  RED→GREEN. The pointer is now the commit authority; cancellation after commit
  leaves the validated new version active and reports completion. Cancellation
  before commit preserves the previous version. Full Python suite: **80/80**.
- Validation: **105 tests total** (80 Python, 24 frontend, 1 native bridge).
  TypeScript/Vite production build, CLI/evaluation startup, Black/isort (47
  Python files), whitespace, local signatures, ZIP checksum and packaged smoke
  checks passed. The final bundled `vikram update --check` reached GitHub and
  correctly reported no stable release. Roughly **64 GiB free** after validation.
- Review limitations: no published asset exists for a real online upgrade;
  transport/update transactions used offline fixtures, and the real ZIP was
  installed in disposable directories. Finder launch/reopen and macOS 13 were
  not exercised. Apple Developer ID signing/notarization and publication remain
  distribution work, explicitly documented rather than claimed complete.
  The owner's CLI link, profiles, chats, models and other applications were not
  changed by validation; no commits, pushes or releases were created.
