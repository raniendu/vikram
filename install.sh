#!/usr/bin/env bash
# install.sh — install vikram on a new machine.
#
# Two paths, auto-detected:
#   1. Run inside an existing vikram checkout:
#      installs from that checkout, no clone.
#   2. Run anywhere else (or piped from curl): clones raniendu/vikram to
#      $VIKRAM_INSTALL_DIR (default ~/.local/share/vikram) via `gh repo clone`,
#      then installs from there.
#
# In both cases the package is installed as an isolated `uv tool`, which
# exposes `vikram` and `vikram-api` on PATH.

set -euo pipefail

REPO="raniendu/vikram"
INSTALL_DIR="${VIKRAM_INSTALL_DIR:-$HOME/.local/share/vikram}"
PYTHON_VERSION="${VIKRAM_PYTHON_VERSION:-3.13}"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }

# 1. Locate or fetch the source tree ----------------------------------------

source_dir=""
script_path="${BASH_SOURCE[0]:-}"
if [ -n "$script_path" ] && [ -f "$script_path" ]; then
  candidate="$(cd "$(dirname "$script_path")" && pwd)"
  while [ "$candidate" != "/" ]; do
    if [ -f "$candidate/pyproject.toml" ] && [ -d "$candidate/vikram" ]; then
      source_dir="$candidate"
      break
    fi
    candidate="$(dirname "$candidate")"
  done
fi

if [ -z "$source_dir" ]; then
  bold "Fetching $REPO into $INSTALL_DIR"
  if ! command -v gh >/dev/null 2>&1; then
    err "gh (GitHub CLI) is required to clone the repo."
    err "Install from https://cli.github.com/ and rerun."
    exit 1
  fi
  if ! gh auth status >/dev/null 2>&1; then
    err "gh is not authenticated. Run: gh auth login"
    exit 1
  fi
  mkdir -p "$(dirname "$INSTALL_DIR")"
  if [ -d "$INSTALL_DIR/.git" ]; then
    if [ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]; then
      warn "$INSTALL_DIR has uncommitted changes; skipping pull."
    else
      info "Updating existing checkout"
      git -C "$INSTALL_DIR" pull --ff-only
    fi
  else
    gh repo clone "$REPO" "$INSTALL_DIR"
  fi
  source_dir="$INSTALL_DIR"
else
  bold "Using existing checkout at $source_dir"
fi

vikram_dir="$source_dir"
spec_root="$vikram_dir/spec"

if [ ! -f "$vikram_dir/pyproject.toml" ]; then
  err "Expected $vikram_dir/pyproject.toml; aborting."
  exit 1
fi

# 2. Ensure uv is installed -------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
  bold "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 3. Install vikram as an isolated uv tool -----------------------------------

bold "Installing vikram (Python $PYTHON_VERSION) from $vikram_dir"
# --reinstall-package vikram forces uv to rebuild the wheel from $vikram_dir on
# every install. Without it, uv reuses a cached wheel for vikram==0.1.0 even
# when the source contents have changed, so updates would silently no-op.
uv tool install \
  --force \
  --reinstall-package vikram \
  --python "$PYTHON_VERSION" \
  --from "$vikram_dir" \
  vikram

# 4. Record install metadata for `vikram update` -----------------------------

meta_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vikram"
meta_file="$meta_dir/install.toml"
mkdir -p "$meta_dir"

git_sha=""
if [ -d "$source_dir/.git" ]; then
  git_sha="$(git -C "$source_dir" rev-parse HEAD 2>/dev/null || true)"
fi
installed_at="$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")"

{
  printf '# Written by vikram install/update — do not edit by hand.\n'
  printf 'source_dir = "%s"\n' "$source_dir"
  printf 'installed_at = "%s"\n' "$installed_at"
  printf 'python_version = "%s"\n' "$PYTHON_VERSION"
  if [ -n "$git_sha" ]; then
    printf 'git_sha = "%s"\n' "$git_sha"
  fi
} > "$meta_file"

if bin_dir="$(uv tool dir --bin 2>/dev/null)"; then
  :
else
  bin_dir="$HOME/.local/bin"
fi

# 5. Configure the local model ----------------------------------------------

config_file="$meta_dir/config.toml"
bold "Model configuration"
info "Vikram has no default model provider or model name."

if [ -t 0 ]; then
  if [ -f "$config_file" ]; then
    info "Existing local config: $config_file"
    printf 'Reconfigure model settings now? [y/N] '
    read -r configure_answer
    configure_answer="${configure_answer:-n}"
  else
    printf 'Configure model settings now? [Y/n] '
    read -r configure_answer
    configure_answer="${configure_answer:-y}"
  fi

  case "$configure_answer" in
    y|Y|yes|YES)
      if "$bin_dir/vikram" configure; then
        :
      else
        warn "Model configuration was not written. Run: $bin_dir/vikram configure"
      fi
      ;;
    *)
      warn "Skipped model configuration. Run before first chat: $bin_dir/vikram configure"
      ;;
  esac
else
  warn "No interactive stdin; run before first chat: $bin_dir/vikram configure"
fi

# 6. Post-install summary ---------------------------------------------------

bold "Installed."
info "Binaries:    $bin_dir/vikram, $bin_dir/vikram-api"
info "Spec root:   $spec_root"
info "Source tree: $source_dir"
info "Metadata:    $meta_file"
info "Config:      $config_file"

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *)
    bold "Add $bin_dir to PATH:"
    printf '  export PATH="%s:$PATH"\n' "$bin_dir"
    ;;
esac

state_dir="${VIKRAM_STATE_DIR:-$HOME/.vikram}"

bold "Required env (add to ~/.zshrc or ~/.bashrc):"
cat <<EOF
  export VIKRAM_SPEC_ROOT="$spec_root"
  export VIKRAM_DB_PATH="$state_dir/vikram.sqlite3"
  export DBOS_SYSTEM_DATABASE_URL="sqlite:///$state_dir/dbos.sqlite3"
EOF

bold "Model config:"
info "Run or rerun: $bin_dir/vikram configure"
info "Env vars like VIKRAM_MODEL_PROVIDER and VIKRAM_MODEL still override $config_file."

bold "Optional (see $vikram_dir/.env.example for the full list):"
info "PARALLEL_API_KEY, VIKRAM_TELEGRAM_BOT_TOKEN, VIKRAM_TELEGRAM_WEBHOOK_SECRET, ..."

bold "Smoke test:"
info "vikram --version"
info "vikram --once --prompt 'say pong'"

bold "Updating later:"
info "vikram update           # fast-forward + reinstall from $source_dir"
info "vikram update --check   # show what would change"
