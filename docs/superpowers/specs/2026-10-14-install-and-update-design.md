# Install and update Vikram

The owner requested one installer for the CLI and Mac app, and `vikram update`
to update both to the latest version. This is an implementation extension to the
approved local chat app; model/tool behavior and chat storage stay shared.

## Delivery

Build `Vikram-VERSION-macos-arm64.zip`, containing **Install Vikram.app**. Open
the installer once to install both components for the current account, without
administrator access, Python, Node, uv, or Rust. Ollama remains a prerequisite;
the installer does not download models. First release: Apple Silicon, macOS 13+.

The installer places a Mac app link at `~/Applications/Vikram.app` and a CLI
launcher at `~/.local/bin/vikram`. Both resolve through the same atomic `current`
link under `~/Library/Application Support/dev.raniendu.vikram.install/`. The CLI
runs the Python runtime already inside the app, so a release cannot mix CLI and
desktop code. Add the CLI directory once to the normal zsh/bash login profile;
existing terminals may need reopening. Unsupported/custom shell setups receive
the explicit command path. Do not edit unrelated shell configuration.

Recognize and preserve the existing uv-managed `vikram` link as
`~/.local/bin/vikram.before-vikram-installer`. Do not uninstall its environment or
touch Vikram Studio. Refuse unfamiliar files at managed destinations with an
actionable error instead of overwriting them. Chat libraries and Ollama models
are never installation targets.

## Updates

`vikram update` discovers the latest stable GitHub Release of `raniendu/vikram`,
downloads its matching versioned ARM64 installer archive, and installs the same
payload for both interfaces. `vikram update --check` only reports availability;
`vikram --version` reports the running CLI version. Existing prompt syntax is
preserved; use `vikram -- "update"` to send the literal prompt `update`.

Only accept stable `vMAJOR.MINOR.PATCH` tags, the exact expected asset name and
repository URL, a bounded asset size, and GitHub's SHA-256 asset digest. Use HTTPS
with verified certificates, bounded requests and allowlisted GitHub asset
redirects. Reject missing/mismatched checksums, unexpected URLs, unsafe archive
paths or types, duplicate/case-colliding paths, oversized expansion, version
mismatches, invalid app signatures, and failed runtime startup. Network failure,
rate limiting, or no published release leave the installed version intact.
Never downgrade automatically. No background update service or credentials.

Serialize installs; stage and validate the complete candidate before atomically
switching the common pointer. Keep earlier versions so running processes remain
safe and a failed update cannot remove the working release. A running desktop
app adopts an update when reopened; never kill an active chat. A retry repairs
an interrupted first install. Retain at least **20 GiB** free, including space
for downloads, extraction, staging, and temporary Python runtime expansion.

## Distribution boundary

Use the existing ad-hoc signing for the local installer. Verify both bundle
signatures and packaged CLI/sidecar startup. Do not strip quarantine or disable
Gatekeeper. Public download distribution needs Developer ID signing and Apple
notarization; document that prerequisite without claiming it is complete.
No releases currently exist. Creating release artifacts and documenting their
publication is in scope; committing, pushing, publishing, and uploading to Apple
are separate owner actions and are not performed by this task.

## Decisions

Use a small AppKit installer wrapper, not a second chat UI. A system-wide `.pkg`
would require administrative privileges for future updates. Separate pip and
desktop updaters would allow their shared agent behavior to drift. The per-user
bundle avoids both problems with one versioned payload and one update operation.

Sources checked: [GitHub release assets and digest](https://docs.github.com/en/rest/releases/assets),
[latest releases](https://docs.github.com/en/rest/releases/releases#get-the-latest-release),
[Apple distribution](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution).
