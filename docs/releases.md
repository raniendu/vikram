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
The first build downloads the Python sandbox image pinned in `vikram/sandbox.py`
(about 20 MB, verified digest by digest) into `build/sandbox/` and bundles it,
so the installed app never fetches it. Later builds reuse the verified copy.
The build also writes the sandbox's
[third-party notices](#third-party-notices-and-sources) into the app, and stops
if the image or VM runtime changed since `scripts/third-party/sources.json` was
made.

```sh
uv run python scripts/build_installer.py
uv run python scripts/check_installer.py dist/installers/Vikram-VERSION-macos-arm64.zip
```

`VERSION` is the release version, which
`uv run python scripts/bump_version.py --check` prints. The build produces the
versioned ZIP, its `.sha256` checksum, and a clickable
`dist/installers/Install Vikram.app`. `--skip-build` packages a previously built
app, checking its version, signature, runtime and notices first. The smoke check
extracts the actual ZIP, checks the app's third-party notices, and exercises
installation, reinstallation, bundled CLI startup, the bundled Python sandbox (it
boots a VM), the bundled browser (it opens a blank page in a hidden WebKit
window, without the network), and the desktop protocol in a disposable
directory. It does not install into the owner's account or contact
Ollama/Parallel.

All four versions must agree: `pyproject.toml`,
`apps/desktop/package.json`, `apps/desktop/src-tauri/Cargo.toml`, and
`apps/desktop/src-tauri/tauri.conf.json`, and so must the uv, npm and Cargo
lockfiles. `uv run python scripts/bump_version.py X.Y.Z` sets a new version in all
seven files. It changes only the version strings, keeps every dependency pin, and
refuses a version that is not newer. The Settings dialog shows the version from
`package.json`. The build also checks the installed Python metadata; run
`uv sync --locked` after a version change. Each published stable version is
unique: use a new version for every changed release rather than replacing assets
on an existing tag. Updates never downgrade or replace an already current version.

## Publish a release

The owner chose this process on 2026-10-21: a version tag starts a release, the
build runs on the owner's Apple Silicon Mac as a self-hosted GitHub Actions
runner, releases are ad-hoc signed (see
[Signing and distribution](#signing-and-distribution)), and a script bumps the
version. GitHub's own Apple Silicon runners cannot build releases: they do not
support nested virtualization, so they cannot boot the sandbox VM that the build
and installer checks start.

Pushing a tag such as `v0.2.0` on a commit in `main` runs
[`.github/workflows/release.yml`](../.github/workflows/release.yml) in two jobs:

1. **Build and check on the release Mac**, with a read-only token. It checks that
   the tag is on `main` and matches every version file and lockfile, runs the
   offline Python and frontend tests, builds with `scripts/build_installer.py`,
   checks the ZIP with `scripts/check_installer.py`, runs the native tests,
   checks the archive against the updater's rules, and downloads every
   third-party source the app's notices list, checking each against its pinned
   digest and packing them as `Vikram-X.Y.Z-third-party-sources.tar`. It hands
   the ZIP, its checksum, and the sources archive to the next job, then deletes
   its build directories. The sources are downloaded here because Alpine's
   GitLab refuses GitHub's hosted runners (HTTP 418, since October 2026).
2. **Publish the release**, on a GitHub-hosted runner: the only job that can
   write. `scripts/publish_release.py --sources` first checks the sources
   archive offline: exactly the files the archive's notices list, each with its
   pinned size and digest, and nothing else. Then it creates a draft release; uploads `Vikram-X.Y.Z-macos-arm64.zip`, its
   `.sha256`, `Vikram-X.Y.Z-THIRD-PARTY-NOTICES.md` and
   `Vikram-X.Y.Z-third-party-sources.tar`; compares GitHub's stored copy of each
   (and its SHA-256 `digest`) with the build's; and only then publishes the
   draft as the latest release. Finally it reads the latest release back with
   the updater's own parser. A failed attempt leaves at most an unpublished
   draft, which a rerun reuses. A published release is never changed.

A manual run (**Actions › Release › Run workflow**, or
`gh workflow run release.yml --ref BRANCH`) is a dry run: it builds and checks the
chosen branch and publishes nothing. GitHub offers manual runs once the workflow
is on `main`.

### Set up the release Mac once

1. In the repository's **Settings › Actions › Runners**, choose **New self-hosted
   runner**, **macOS** and **ARM64**. Follow GitHub's download and configure
   commands in a folder such as `~/actions-runner`; when `config.sh` asks for
   additional labels, enter `vikram-release`. Skip installing it as a service.
2. In **Settings › Actions › General**, set the approval for fork pull request
   workflows to **Require approval for all external contributors**. The
   repository is public, and a pull request can edit workflow files to run on
   the release Mac; approval lets you read such a change before anything runs.
   Vikram's own pull request checks run on GitHub-hosted runners.
3. Optionally, under **Settings › Environments › release** (created by the first
   publish, or create it yourself), add yourself as a required reviewer so each
   publish waits for your approval, and allow only tags matching `v*`.

The runner runs jobs as your macOS account, with the `PATH` of the terminal that
starts it. That terminal needs the README's build tools: uv, Node 22.12 or newer,
Rust, and the Xcode command-line tools. Stay logged in, because the browser check
opens a hidden WebKit window. A build needs 24 GiB free to keep the 20 GiB
reserve. Start the runner only while releasing, so nothing else can use it.

### Cut a release

Each release needs a version newer than any installed copy. The first local
installers were 0.1.0, so the first release must be 0.1.1 or later. Bump the
version on a branch:

```sh
git switch -c release-0.2.0 origin/main
uv run python scripts/bump_version.py 0.2.0
uv sync --locked
git commit -am "Release 0.2.0"
```

Merge it through a pull request. Then start the runner (`./run.sh` in its
folder), tag the merge commit, and push the tag:

```sh
git fetch origin
git tag v0.2.0 origin/main
git push origin v0.2.0
```

Follow the run under **Actions › Release**. When it succeeds,
`vikram update --check` reports the new version and `vikram update` installs it.
Stop the runner with Ctrl+C.

If the build job fails, nothing was published. Fix the problem through a pull
request, delete the tag (`git push origin :refs/tags/v0.2.0`, then
`git tag -d v0.2.0`), and tag the new merge commit. If only the publish job
fails, use **Re-run failed jobs** within seven days, while the build's archive is
kept. Never replace or edit the assets of a published release; release a new
version instead.

## Signing and distribution

Releases are **ad-hoc signed and not notarized**. On 2026-10-21 the owner chose
to publish them this way, for updating their own Macs, until Developer ID signing
is set up. `vikram update` downloads with its own HTTPS client, so macOS does not
quarantine the archive, and it verifies GitHub's digest, the code signatures and
the bundled runtime before switching versions. A copy downloaded in a web browser
is quarantined, and Gatekeeper may refuse to open it. Do not disable Gatekeeper
or strip quarantine attributes.

Before offering releases as a public download, configure Developer ID signing
for the Tauri app, its embedded Python executable/libraries, the sandbox's VM
runtime, and the installer; configure and test Hardened Runtime entitlements for
bundled Python; then notarize and staple the distribution using Apple's tools. The VM helper `smol-vmm` needs its
`com.apple.security.hypervisor` and `com.apple.security.cs.disable-library-validation`
entitlements: the build ships it unmodified because PyInstaller's re-signing
strips them, so production signing must re-apply them. The current build script
deliberately uses local signing and is not a production signing pipeline. See
[Apple's packaging guidance](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution)
and [notarization guidance](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution).
Because the release Mac builds every release, its keychain can hold the signing
identity and notarization credentials without adding them to GitHub.

Each release also carries the notices and the source code of the sandbox's
third-party components: see
[Third-party notices and sources](#third-party-notices-and-sources).

Every release uses tag `vMAJOR.MINOR.PATCH` and the exact asset
`Vikram-MAJOR.MINOR.PATCH-macos-arm64.zip`, and becomes the stable latest release
only after its assets are uploaded and checked. GitHub supplies the SHA-256
`digest` in its [release asset API](https://docs.github.com/en/rest/releases/assets);
the updater requires and verifies it. Checksums establish integrity against the
trusted repository metadata; they do not substitute for Apple notarization.
HTTPS, bounded redirects, extraction limits and local signature checks also apply.

Until the first stable release is published, `vikram update --check` reports that
there is none. Building artifacts locally never commits, pushes, publishes, or
uploads anything to Apple; only a pushed version tag publishes a release.

## Third-party notices and sources

The Python sandbox includes GPL, LGPL and MPL software: the Linux kernel in
smolmachines' libkrunfw, Alpine packages in the VM's guest filesystem, and
Alpine packages in the pinned Python image. On 2026-10-23 the owner chose to
attach the exact source of those components to every release, rather than link
to upstream copies.

- **Notices in the app.** The desktop build writes
  `Vikram.app/Contents/Resources/notices/`: `THIRD-PARTY-NOTICES.md` (each
  component, its version, license and source), `third-party-sources.json`, and
  the license texts, including smolmachines' own notices and the CPython and pip
  licenses copied from the image. Installed and updated copies keep them.
- **Sources beside the archive.** Publishing downloads every file that
  `third-party-sources.json` lists, checks its pinned SHA-256 or SHA-512, and
  attaches `Vikram-X.Y.Z-third-party-sources.tar` (about 190 MiB, mostly the
  kernel) and `Vikram-X.Y.Z-THIRD-PARTY-NOTICES.md` to the release. It reads the
  list from the archive it publishes, so the sources always match that build.
  If any file is missing or different, nothing is published. The installer ZIP
  stays under the updater's 256 MiB limit because the sources are a separate
  asset.
- **Pinned in the repository.** `scripts/third-party/sources.json` records the
  image digest, the smolmachines version and the digests of its kernel library,
  guest filesystem and notices. For each component it pins every source file:
  the kernel release from kernel.org; libkrunfw at the commit smolmachines
  names, with the kernel configuration and patches; and for each Alpine package,
  its aports recipe and patches at the commit its package database records,
  plus the upstream files from Alpine's source mirror.
  `scripts/third-party/licenses/` holds the SPDX text of every license the
  packages name.

After a change to the image pin in `vikram/sandbox.py` or to the smolmachines
version, the build stops until the list is refreshed. With the new smolmachines
installed (`uv sync --locked`), run:

```sh
uv run python scripts/third_party.py refresh
```

It fetches the pinned image if needed. It needs the network and, before
Python 3.14, the `zstd` command (`brew install zstd`) to read the guest
filesystem. Review and commit the
changes to `scripts/third-party/`. To check a built archive's sources without
publishing anything, run
`uv run python scripts/third_party.py sources --check ARCHIVE`; without
`--check`, it downloads them (about 190 MiB) next to the archive.
