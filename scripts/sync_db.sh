#!/usr/bin/env bash
#
# scripts/sync_db.sh — sync data/rli.db between this laptop and a remote
# cloud VM over ssh+rsync. Runs FROM the laptop (the control machine).
#
# Rather than rsyncing the live SQLite file directly (risky if it's mid-write
# under WAL mode), each direction first takes a consistent point-in-time
# snapshot on the SOURCE side with SQLite's `VACUUM INTO`, which works
# cleanly against a live WAL-mode DB without requiring anything to stop, and
# transfers that snapshot instead.
#
# Usage:
#   scripts/sync_db.sh pull <vm-ssh-host> [vm-repo-path]
#   scripts/sync_db.sh push <vm-ssh-host> [vm-repo-path]
#
#   <vm-ssh-host>   anything ssh accepts: user@1.2.3.4, or an ssh-config alias
#   [vm-repo-path]  defaults to ~/job_liveliness_investigator, matching
#                   setup_cloud_vm.sh's clone location

set -euo pipefail

# --- 1: reach the local repo root (for the local side of pull/push) --------
cd "$(dirname "$0")/.."

# --- 2: validate args --------------------------------------------------------
SUBCOMMAND="${1:-}"
VM_HOST="${2:-}"
VM_REPO_PATH="${3:-~/job_liveliness_investigator}"

usage() {
  echo "Usage: $0 pull|push <vm-ssh-host> [vm-repo-path]" >&2
  echo "  vm-repo-path defaults to ~/job_liveliness_investigator" >&2
}

if [ "$SUBCOMMAND" != "pull" ] && [ "$SUBCOMMAND" != "push" ]; then
  echo "Error: subcommand must be 'pull' or 'push' (got: '${SUBCOMMAND}')" >&2
  usage
  exit 2
fi
if [ -z "$VM_HOST" ]; then
  echo "Error: <vm-ssh-host> is required" >&2
  usage
  exit 2
fi

LOCAL_DB="data/rli.db"
# Remote paths are expanded by the remote shell (ssh runs a shell there), so
# ~ in VM_REPO_PATH resolves correctly even though we never expand it here.
REMOTE_DB="$VM_REPO_PATH/data/rli.db"

if [ "$SUBCOMMAND" = "pull" ]; then
  # ---- pull: VM is the SOURCE, laptop is the DESTINATION -------------------
  REMOTE_SNAPSHOT="/tmp/rli-sync-$(date -u +%s).db"
  LOCAL_TMP="$LOCAL_DB.tmp"

  echo "==> Taking consistent snapshot on $VM_HOST ..."
  ssh "$VM_HOST" "sqlite3 '$REMOTE_DB' \"VACUUM INTO '$REMOTE_SNAPSHOT'\""

  # Always clean up the remote snapshot, even if rsync fails partway.
  cleanup_remote() { ssh "$VM_HOST" "rm -f '$REMOTE_SNAPSHOT'" || true; }
  trap cleanup_remote EXIT

  echo "==> Sizes before transfer:"
  ssh "$VM_HOST" "ls -lh '$REMOTE_SNAPSHOT'"

  echo "==> Transferring snapshot to $LOCAL_TMP ..."
  rsync -avz --progress "$VM_HOST:$REMOTE_SNAPSHOT" "$LOCAL_TMP"

  # Atomic rename onto the working DB: a partial transfer never corrupts it,
  # since mv within the same filesystem is atomic.
  mv "$LOCAL_TMP" "$LOCAL_DB"

  echo "==> Sizes after transfer:"
  ls -lh "$LOCAL_DB"
  echo "Pull complete: $VM_HOST:$REMOTE_DB -> $LOCAL_DB"

else
  # ---- push: laptop is the SOURCE, VM is the DESTINATION --------------------
  LOCAL_SNAPSHOT="$(mktemp /tmp/rli-sync-XXXXXX.db)"
  cleanup_local() { rm -f "$LOCAL_SNAPSHOT"; }
  trap cleanup_local EXIT

  echo "==> Taking consistent local snapshot ..."
  rm -f "$LOCAL_SNAPSHOT"   # sqlite3 refuses to VACUUM INTO an existing file
  sqlite3 "$LOCAL_DB" "VACUUM INTO '$LOCAL_SNAPSHOT'"

  echo "==> Sizes before transfer:"
  ls -lh "$LOCAL_SNAPSHOT"

  # rsync straight to a remote temp path inside the VM repo's data/ dir (same
  # filesystem as the final destination, so the remote mv below is atomic),
  # then ssh in and mv it onto the live DB. The temp name is unique per run
  # (pid), so no separate remote cleanup step is needed: the mv consumes it.
  REMOTE_TMP="$VM_REPO_PATH/data/rli.db.tmp.$$"
  echo "==> Transferring snapshot to $VM_HOST:$REMOTE_TMP ..."
  rsync -avz --progress "$LOCAL_SNAPSHOT" "$VM_HOST:$REMOTE_TMP"

  echo "==> Moving into place atomically on $VM_HOST ..."
  ssh "$VM_HOST" "mv '$REMOTE_TMP' '$REMOTE_DB'"

  echo "==> Sizes after transfer:"
  ssh "$VM_HOST" "ls -lh '$REMOTE_DB'"
  echo "Push complete: $LOCAL_DB -> $VM_HOST:$REMOTE_DB"
fi
