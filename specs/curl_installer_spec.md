# Spec: Zero-Auth Curl Installer (Feature Design)

## Goal
Enable installing Vikram using a single unauthenticated pipe command on any standard Unix-like machine:
```bash
curl -LsSf https://raw.githubusercontent.com/raniendu/vikram/main/install.sh | bash
```
...without requiring `gh` (GitHub CLI), interactive authentication, or prior repo checkouts.

## Background
Currently, `install.sh` strictly requires `gh clone`; it fails with a fatal error if `gh` is missing or unauthenticated. This prevents running the script as a pure `curl` pipe on brand-new machines, CI runners, or in environments without GitHub CLI tokens set up. 

While `vikram` supports many installation patterns (e.g., Docker, editable install), the zero-auth curl installer reduces friction for developers getting started locally.

## New Logic: `curl` Fallback
When no existing checkout is found (`if [ -z "$source_dir" ]`), the installer will:
1. Check if `gh` is available and authenticated. If so, use it to clone (existing fast/encrypted behavior).
2. Otherwise, fall back to downloading the repository source as a `.tar.gz` from GitHub's CDN using `curl`.
3. Extract the tarball into `$VIKRAM_INSTALL_DIR` using standard `bash`, `mkdir`, and `curl | tar` pipes.
4. Proceed with existing `uv tool install` logic, as `$INSTALL_DIR/pyproject.toml` will now exist.

## Implementation Details
- **Dependency Chain**: This enables the installer to depend on zero external CLI tools besides what is needed by `uv`. `bash`, `curl`, `tar`/`unzip`, and `uv` are the only strictly required tools.
- **Directory Safety**: The script creates `$INSTALL_DIR` safely. If piping the curl script multiple times, it detects existing files and clears them atomically to prevent mixing local artifacts with remote extraction garbage.
- **Security Considerations**: The download URL (`https://github.com/<repo>/archive/...`) is hardcoded in the script. This matches standard practices for bootstrap installers. `curl -sSf` ensures failure on HTTP errors rather than executing binary garbage.

## Expected User Flow (Zero-Auth)
A user executes:
```bash
curl -LsSf https://raw.githubusercontent.com/raniendu/vikram/main/install.sh | bash
> Fetching raniendu/vikram into /Users/homer/.local/share/vikram
> No GitHub CLI auth found; fetching repository archive via curl.
> Installed from github archive (no local .git directory).
> Installing uv ...
> Installing vikram (Python 3.13) from /Users/homer/.local/share/vikram
...
```

## Requirements Completed
- [ ] Documented in `specs/curl_installer_spec.md` (this file).
- [x] Added curl fallback block in `install.sh` before the `gh` check.
- [x] Updated `$INSTALL_DIR` path generation to safely receive tarball extraction without mixing local/remote files.
- [x] Updated `README.md` to feature the one-liner prominently under "Zero-Auth Install".
