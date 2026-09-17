#!/usr/bin/env bash
#
# setup_laptop_timer.sh
#
# Installs (or uninstalls) a systemd USER timer that runs scripts/daily.sh
# once a day on this laptop. Uses systemd --user units + `loginctl
# enable-linger` so the timer fires even when the user isn't logged in
# graphically, and Persistent=true so a missed run (laptop off) catches up
# at the next boot/login.
#
# Usage:
#   scripts/setup_laptop_timer.sh              # install + enable the timer
#   scripts/setup_laptop_timer.sh --uninstall  # disable + remove the timer
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
SERVICE_NAME="rli-daily.service"
TIMER_NAME="rli-daily.timer"

usage() {
  echo "Usage: $0 [--uninstall]" >&2
}

uninstall() {
  echo "Disabling and removing ${TIMER_NAME}..."
  systemctl --user disable --now "${TIMER_NAME}" || true

  rm -f "${UNIT_DIR}/${TIMER_NAME}"
  rm -f "${UNIT_DIR}/${SERVICE_NAME}"

  systemctl --user daemon-reload

  echo "Uninstalled ${TIMER_NAME} and ${SERVICE_NAME}."
  echo "Note: linger is left enabled (not touched by --uninstall)." \
       "To disable it yourself, run: loginctl disable-linger \"$USER\""
}

install() {
  mkdir -p "${UNIT_DIR}"

  # systemd --user services get a minimal PATH that usually excludes
  # ~/.local/bin, where `uv` normally lives. daily.sh calls `uv run ...`,
  # so bake an explicit PATH into the service unit that includes uv's dir.
  local uv_path
  uv_path="$(command -v uv || true)"
  local uv_dir
  if [[ -n "${uv_path}" ]]; then
    uv_dir="$(dirname "${uv_path}")"
  else
    echo "WARNING: 'uv' was not found on PATH while generating ${SERVICE_NAME}." \
         "Falling back to \$HOME/.local/bin in the unit's PATH, but if uv" \
         "actually lives elsewhere, daily.sh will fail to find it at run time." >&2
    uv_dir="${HOME}/.local/bin"
  fi
  local service_path_env="${uv_dir}:/usr/local/bin:/usr/bin:/bin"

  cat > "${UNIT_DIR}/${SERVICE_NAME}" <<EOF
[Unit]
Description=Run job_liveliness_investigator daily snapshot (rli-daily)

[Service]
Type=oneshot
WorkingDirectory=${REPO_DIR}
Environment=PATH=${service_path_env}
ExecStart=${REPO_DIR}/scripts/daily.sh
EOF

  cat > "${UNIT_DIR}/${TIMER_NAME}" <<EOF
[Unit]
Description=Daily timer for job_liveliness_investigator snapshot (rli-daily)

[Timer]
OnCalendar=*-*-* 00:05:00
OnCalendar=*-*-* 12:05:00
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
EOF

  echo "Wrote ${UNIT_DIR}/${SERVICE_NAME}"
  echo "Wrote ${UNIT_DIR}/${TIMER_NAME}"

  systemctl --user daemon-reload
  systemctl --user enable --now "${TIMER_NAME}"

  echo "Enabled and started ${TIMER_NAME} (this only arms the timer;" \
       "it does NOT run ${SERVICE_NAME} immediately)."

  loginctl enable-linger "$USER" 2>/dev/null || {
    echo "WARNING: linger could not be enabled automatically; run" \
         "\`loginctl enable-linger $USER\` yourself, or the timer will" \
         "only fire while you're logged in." >&2
  }

  cat <<EOF

Install complete. Useful commands:
  systemctl --user list-timers ${TIMER_NAME}
  journalctl --user -u ${SERVICE_NAME%.service} -n 50
  loginctl show-user "$USER" -p Linger
EOF
}

main() {
  local mode="install"
  if [[ $# -gt 0 ]]; then
    case "$1" in
      --uninstall)
        mode="uninstall"
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        usage
        exit 1
        ;;
    esac
  fi

  if [[ "${mode}" == "uninstall" ]]; then
    uninstall
  else
    install
  fi
}

main "$@"
