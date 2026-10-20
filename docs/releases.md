# Installing and releasing Vikram

## Install both interfaces

Apple Silicon and macOS 13+ are supported. Unzip
`Vikram-VERSION-macos-arm64.zip`, then open **Install Vikram.app** once. It installs
the desktop app and CLI together for your account, without administrator access
or a separate Python/uv/Node/Rust installation. Start Ollama and install a local
model separately. The installer never changes models or the chat library.

Open `~/Applications/Vikram.app`, or open a new terminal and run:

```sh
vikram --version
vikram
vikram update --check
vikram update
```

The CLI launcher is `~/.local/bin/vikram`. The installer adds that directory to
the normal zsh or bash login profile once. Existing terminals may need reopening
or `export PATH="$HOME/.local/bin:$PATH"`. Custom `ZDOTDIR` and other shells need
their normal PATH setup; the absolute command path always works. A linked shell
profile is left untouched and reported as an installation conflict, so preserve
your custom configuration before resolving it. Unknown existing app/CLI files
are never overwritten. The known uv tool symlink is preserved beside the new
command as `vikram.before-vikram-installer`; its environment is retained.

`vikram update` updates both interfaces to the latest stable release of
[`raniendu/vikram`](https://github.com/raniendu/vikram/releases). It does not run
`git pull`, update a developer checkout, change uv environments, or use PyPI.
The source checkout also supports the command; install the managed bundle first
to apply updates. `--check` works before installation and changes no files.
Use `vikram -- update` to send the literal chat prompt `update`.

A running desktop app continues using its existing files until you quit and
reopen it. CLI and app launch paths switch together only after checksum,
signature, version and startup checks pass. Interrupted downloads leave the
working version intact. Rerun the installer after an interrupted first install.
No background service, automatic model download, administrator access, or API
credentials are involved.

Managed versions live under
`~/Library/Application Support/dev.raniendu.vikram.install/`. Old versions are
retained so running processes can finish safely. They consume roughly one app
bundle per update; there is no automatic pruning yet. Downloads and installation
reserve at least 20 GiB plus temporary runtime headroom. Chats remain in the
separate `~/Library/Application Support/dev.raniendu.vikram/` directory.

## Build a local installer

From an ARM64 macOS development checkout with the dependencies in the README:

Quit any copy running from the checkout's build directory first. Rebuilding a
running PyInstaller executable can invalidate deferred imports in its existing
process. The build checks for that condition and stops before replacing files.
An installed version uses its own immutable directory and can remain open.

```sh
uv run python scripts/build_installer.py
uv run python scripts/check_installer.py dist/installers/Vikram-0.1.0-macos-arm64.zip
```

The build produces the versioned ZIP, its `.sha256` checksum, and a clickable
`dist/installers/Install Vikram.app`. `--skip-build` packages a previously built
app, checking its version, signature and runtime first. The smoke check extracts
the actual ZIP and exercises installation, reinstallation, bundled CLI startup,
the bundled Python sandbox, and the desktop protocol in a disposable directory. It does not install into
the owner's account or contact Ollama/Parallel.

All four versions must agree: `pyproject.toml`,
`apps/desktop/package.json`, `apps/desktop/src-tauri/Cargo.toml`, and
`apps/desktop/src-tauri/tauri.conf.json`. Refresh Python/npm/Cargo lockfiles when
bumping them. The build also checks the installed Python metadata; run
`uv sync --locked` after a version change. Each published stable version is
unique: use a new version for every changed release rather than replacing assets
on an existing tag. Updates never downgrade or replace an already current version.

## Public release gate

The current artifact is **ad-hoc signed for local use, not notarized**. A locally
built installer can be used on this Mac. A downloaded copy on another Mac may be
blocked by Gatekeeper. Do not disable Gatekeeper or strip quarantine attributes.

Before publishing a public download, configure Developer ID signing for the
Tauri app, its embedded Python executable/libraries, the bundled `monty` sandbox
worker, and the installer; configure and
test Hardened Runtime entitlements for bundled Python; then notarize and staple
the distribution using Apple's tools. The current build script deliberately
uses local signing and is not a production signing pipeline. See
[Apple's packaging guidance](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)
and [notarization guidance](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution).

Once signing, notarization and validation are complete, an owner-authorized
release should use tag `vMAJOR.MINOR.PATCH` and attach the exact asset
`Vikram-MAJOR.MINOR.PATCH-macos-arm64.zip`. Mark it as a stable latest release only
after every asset is uploaded. GitHub supplies the SHA-256 `digest` in its
[release asset API](https://docs.github.com/en/rest/releases/assets); the updater
requires and verifies it. Checksums establish integrity against the trusted
repository metadata; they do not substitute for Apple notarization. HTTPS,
bounded redirects, extraction limits and local signature checks also apply.

No release has been published by this implementation task. Until the first
stable release is published, `vikram update --check` reports that there is none.
Building artifacts never commits, pushes, publishes, or uploads anything to Apple.
