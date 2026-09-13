#!/usr/bin/env bash
#
# scripts/setup_cloud_vm.sh — bootstrap a fresh Ubuntu/Debian free-tier cloud
# VM (e.g. Oracle Cloud Always Free ARM, or GCP e2-micro) to run this
# project's daily job unattended.
#
# This script runs ON THE VM, not on your laptop, and the repo is NOT yet
# cloned when it first runs — so before running it, get this one file onto
# the VM by either:
#   scp scripts/setup_cloud_vm.sh <vm-host>:~/setup_cloud_vm.sh
# or, once it's pushed to GitHub:
#   curl -LsSf https://raw.githubusercontent.com/<owner>/<repo>/main/scripts/setup_cloud_vm.sh -o setup_cloud_vm.sh
#
# Then on the VM:
#   chmod +x setup_cloud_vm.sh
#   ./setup_cloud_vm.sh <git-repo-url> [path-to-local-.env-to-copy]
#
# It is safe to re-run: cloning, cron install, etc. are all idempotent.

set -euo pipefail

# --- 1/2: validate args -----------------------------------------------------
REPO_URL="${1:-}"
ENV_SRC="${2:-}"
if [ -z "$REPO_URL" ]; then
  echo "Usage: $0 <git-repo-url> [path-to-local-.env-to-copy]" >&2
  exit 2
fi

REPO_DIR="$HOME/job_liveliness_investigator"

# --- 3: install OS prerequisites --------------------------------------------
# sudo is expected and appropriate here: this is a fresh remote VM being
# provisioned, not a personal laptop.
sudo apt-get update && sudo apt-get install -y git curl ca-certificates

# --- 4: install uv -----------------------------------------------------------
curl -LsSf https://astral.sh/uv/install.sh | sh
# Make uv available for the rest of THIS script/session. The official
# installer usually also wires this into your shell rc file (e.g. .bashrc)
# for future interactive logins — if it didn't, add the same line yourself.
export PATH="$HOME/.local/bin:$PATH"

# --- 5: clone or update the repo, idempotently ------------------------------
if [ -d "$REPO_DIR/.git" ]; then
  (cd "$REPO_DIR" && git pull)
else
  git clone "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"

# --- 6: optionally install a local .env onto the VM -------------------------
if [ -n "$ENV_SRC" ]; then
  cp "$ENV_SRC" .env
  chmod 600 .env
  echo "WARNING: .env copied to $REPO_DIR/.env — it holds API keys" \
       "(e.g. GEMINI_API_KEY / MISTRAL_API_KEY per repo convention)." \
       "Keep it private (mode 600 already applied)." >&2
fi

# --- 7: initialize the DB and load targets ----------------------------------
uv run rli init-db --path data/rli.db
uv run rli load-targets --targets scripts/targets.csv --db data/rli.db

# --- 8: make sure the daily job is executable -------------------------------
chmod +x scripts/daily.sh

# --- 9: install a system cron entry for the daily job, idempotently --------
# Fixed UTC time (any reasonable fixed time works; UTC avoids DST drift on a
# headless VM). Chosen here: 06:07 UTC.
#
# Cron's own PATH is minimal, but daily.sh invokes `uv run`, so we prepend
# $HOME/.local/bin (where the uv installer puts `uv`) via an explicit PATH
# line at the top of the crontab content. This is simpler than hardcoding
# uv's absolute path inside daily.sh (which we don't own/edit) or inside the
# cron line itself, and it's future-proof if other tooling also lands in
# ~/.local/bin.
CRON_PATH_LINE="PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
CRON_JOB="6 7 * * * cd $REPO_DIR && ./scripts/daily.sh"
{
  crontab -l 2>/dev/null | grep -v "scripts/daily.sh" | grep -v "^PATH="
  echo "$CRON_PATH_LINE"
  echo "$CRON_JOB"
} | crontab -

# --- 10: summary -------------------------------------------------------------
echo
echo "Setup complete."
echo "  Repo:        $REPO_DIR"
echo "  DB:          $REPO_DIR/data/rli.db"
echo "  Cron:        daily.sh scheduled at 06:07 UTC"
echo
echo "Verify with:"
echo "  crontab -l"
echo "  tail \"$REPO_DIR/data/logs/daily-\$(date -u +%Y%m%d).log\""
