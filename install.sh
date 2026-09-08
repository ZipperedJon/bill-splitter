#!/usr/bin/env bash
#
# Bill Splitter - one-script installer for a Raspberry Pi (or any Debian box).
#
#   curl -fsSL https://raw.githubusercontent.com/ZipperedJon/bill-splitter/main/install.sh | bash
#
# or, from a checkout:
#
#   ./install.sh
#
# Safe to re-run: it upgrades an existing install in place and never touches
# your database. Uninstall with ./install.sh --uninstall (which keeps data/).

set -euo pipefail

# --- settings (override with environment variables) --------------------------
REPO="${BILLSPLIT_REPO:-ZipperedJon/bill-splitter}"          # rewritten by the repo you clone from
BRANCH="${BILLSPLIT_BRANCH:-main}"
PORT="${BILLSPLIT_PORT:-9100}"                # 9000 is taken by another project
INSTALL_DIR="${BILLSPLIT_DIR:-$HOME/bill-splitter}"
SERVICE="${BILLSPLIT_SERVICE:-bill-splitter}"
RUN_USER="${BILLSPLIT_USER:-$(id -un)}"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
RED=$'\033[31m'; BLUE=$'\033[34m'; OFF=$'\033[0m'

say()  { printf '%s\n' "${BLUE}==>${OFF} ${BOLD}$*${OFF}"; }
info() { printf '    %s\n' "${DIM}$*${OFF}"; }
ok()   { printf '    %s\n' "${GREEN}✓${OFF} $*"; }
warn() { printf '    %s\n' "${YELLOW}!${OFF} $*"; }
die()  { printf '%s\n' "${RED}✗ $*${OFF}" >&2; exit 1; }

need_sudo() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else
    command -v sudo >/dev/null 2>&1 || die "sudo not found and not running as root."
    SUDO="sudo"
  fi
}

# --- doctor ------------------------------------------------------------------
# `./install.sh --doctor` answers "what is actually running, and is it current?"
# in one command. Worth having: a browser holding stale cached assets, a service
# started from a different directory, and an update that never fetched all look
# identical from the outside.
if [ "${1:-}" = "--doctor" ]; then
  # Every lookup below is guarded: with `set -euo pipefail` a missing systemctl
  # or a non-repo directory would abort the script mid-diagnosis and print
  # nothing at all, which is the least helpful possible outcome.
  set +e
  set +o pipefail

  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
  [ -z "$SELF_DIR" ] && SELF_DIR="$PWD"
  SVC_DIR="$(systemctl show -p WorkingDirectory --value "$SERVICE" 2>/dev/null | sed 's/^WorkingDirectory=//')"
  [ -z "$SVC_DIR" ] && SVC_DIR="(no systemd unit)"

  printf '\n%s\n\n' "${BOLD}Bill Splitter doctor${OFF}"
  printf '  %-22s %s\n' "this checkout" "$SELF_DIR"
  printf '  %-22s %s\n' "service runs in" "$SVC_DIR"
  if [ "$SVC_DIR" != "(no systemd unit)" ] && [ "$SVC_DIR" != "$SELF_DIR" ]; then
    warn "The service runs somewhere else - updating here changes nothing."
  fi
  printf '  %-22s %s\n' "VERSION file" "$(cat "$SELF_DIR/VERSION" 2>/dev/null || echo '?')"

  if [ -d "$SELF_DIR/.git" ]; then
    printf '  %-22s %s\n' "commit" "$(git -C "$SELF_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
    printf '  %-22s %s\n' "branch" "$(git -C "$SELF_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    printf '  %-22s %s\n' "remote" "$(git -C "$SELF_DIR" remote get-url origin 2>/dev/null || echo 'none')"
    DIRTY="$(git -C "$SELF_DIR" status --porcelain --untracked-files=no | wc -l | tr -d ' ')"
    printf '  %-22s %s\n' "local edits" "$DIRTY file(s)"
    if git -C "$SELF_DIR" fetch -q origin "$BRANCH" 2>/dev/null; then
      BEHIND="$(git -C "$SELF_DIR" rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo '?')"
      printf '  %-22s %s\n' "commits behind" "$BEHIND"
      [ "$BEHIND" != "0" ] && [ "$BEHIND" != "?" ] && \
        warn "Out of date. Run: git -C $SELF_DIR reset --hard origin/$BRANCH && $SELF_DIR/install.sh"
    else
      printf '  %-22s %s\n' "commits behind" "(could not reach GitHub)"
    fi
  else
    warn "Not a git checkout - it cannot update itself. Re-install with install.sh."
  fi

  printf '  %-22s %s\n' "share page present" \
    "$([ -f "$SELF_DIR/static/js/share.js" ] && echo yes || echo 'NO - old version')"
  printf '  %-22s %s\n' "service state" "$(systemctl is-active "$SERVICE" 2>/dev/null || echo 'not installed')"
  printf '  %-22s %s\n' "answering on :$PORT" \
    "$(curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || echo 'no response')"
  printf '\n  %s\n\n' "${DIM}If the served version and the commit above disagree, restart the service.${OFF}"
  exit 0
fi

# --- uninstall ---------------------------------------------------------------
if [ "${1:-}" = "--uninstall" ]; then
  need_sudo
  say "Removing the $SERVICE service"
  $SUDO systemctl disable --now "$SERVICE" 2>/dev/null || true
  $SUDO rm -f "/etc/systemd/system/$SERVICE.service"
  $SUDO systemctl daemon-reload
  ok "Service removed."
  info "Files left in place at $INSTALL_DIR (your database is in $INSTALL_DIR/data)."
  info "Delete them yourself when you are sure: rm -rf $INSTALL_DIR"
  exit 0
fi

printf '\n%s\n' "${BOLD}Bill Splitter installer${OFF}"
printf '%s\n\n' "${DIM}split costs with your group, on your own hardware${OFF}"

# --- 1. dependencies ---------------------------------------------------------
say "Checking prerequisites"

if command -v apt-get >/dev/null 2>&1; then
  MISSING=""
  command -v git >/dev/null 2>&1 || MISSING="$MISSING git"
  command -v python3 >/dev/null 2>&1 || MISSING="$MISSING python3"
  python3 -c "import venv" >/dev/null 2>&1 || MISSING="$MISSING python3-venv"
  if [ -n "$MISSING" ]; then
    need_sudo
    info "Installing:$MISSING"
    $SUDO apt-get update -qq
    # shellcheck disable=SC2086
    $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $MISSING
  fi
else
  command -v git >/dev/null 2>&1 || die "git is required."
  command -v python3 >/dev/null 2>&1 || die "python3 is required."
fi

command -v git >/dev/null 2>&1 || die "git is still missing."
PYTHON="$(command -v python3)"
PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 or newer required (found $PY_VERSION)."
ok "git and Python $PY_VERSION"

# --- 2. code -----------------------------------------------------------------

# Bring a checkout up to date with its remote. Re-running this script is the
# documented way to upgrade, so it has to actually fetch - skipping the pull
# would reinstall the same code and look like the upgrade did nothing.
pull_latest() {
  local dir="$1" before after
  git -C "$dir" remote get-url origin >/dev/null 2>&1 || {
    info "No git remote here, so nothing to pull."
    return 0
  }
  before="$(git -C "$dir" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  if ! git -C "$dir" fetch --prune origin "$BRANCH" 2>/dev/null; then
    warn "Could not reach the remote; carrying on with the code already here."
    return 0
  fi
  if [ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; then
    warn "Local edits found; leaving them alone and not resetting the code."
    info "Commit or discard them, then re-run, to pick up the latest version."
    return 0
  fi
  git -C "$dir" reset --hard "origin/$BRANCH" >/dev/null
  after="$(git -C "$dir" rev-parse --short HEAD)"
  if [ "$before" = "$after" ]; then
    ok "Already up to date ($after)"
  else
    ok "Updated $before -> $after"
  fi
}

# Running from inside a checkout? Use it. Otherwise clone.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/app/main.py" ]; then
  INSTALL_DIR="$SCRIPT_DIR"
  say "Installing from this checkout"
  info "$INSTALL_DIR"
  [ -d "$INSTALL_DIR/.git" ] && pull_latest "$INSTALL_DIR"
elif [ -d "$INSTALL_DIR/.git" ]; then
  say "Updating the existing install"
  pull_latest "$INSTALL_DIR"
else
  [ -z "$REPO" ] && die \
"No repository configured. Either run this script from a checkout, or set the repo:
     BILLSPLIT_REPO=yourname/bill-splitter bash install.sh"
  say "Cloning $REPO"
  git clone --branch "$BRANCH" "https://github.com/$REPO.git" "$INSTALL_DIR"
  ok "Cloned into $INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# --- 3. virtualenv -----------------------------------------------------------
say "Setting up the Python environment"
if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON" -m venv .venv
  info "Created .venv"
fi
./.venv/bin/python -m pip install --quiet --upgrade pip setuptools wheel
./.venv/bin/python -m pip install --quiet -r requirements.txt
ok "Dependencies installed (fastapi, uvicorn - no compiling needed)"

# --- 4. configuration --------------------------------------------------------
say "Writing configuration"
mkdir -p data data/backups

if [ ! -f .env ]; then
  SECRET="$(./.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  cat > .env <<EOF
# Bill Splitter configuration. Restart the service after editing:
#   sudo systemctl restart $SERVICE

BILLSPLIT_PORT=$PORT
BILLSPLIT_HOST=0.0.0.0
BILLSPLIT_SECRET_KEY=$SECRET
BILLSPLIT_SERVICE=$SERVICE

# Set this to serve over https behind a reverse proxy.
BILLSPLIT_COOKIE_SECURE=false

# Where auto-update pulls from. An admin can change this in the UI too.
BILLSPLIT_UPDATE_REPO=$REPO
BILLSPLIT_UPDATE_BRANCH=$BRANCH
EOF
  chmod 600 .env
  ok "Created .env with a freshly generated secret key"
else
  # Keep the existing secret and any hand edits; just make sure the port matches.
  if grep -q '^BILLSPLIT_PORT=' .env; then
    sed -i.bak "s/^BILLSPLIT_PORT=.*/BILLSPLIT_PORT=$PORT/" .env && rm -f .env.bak
  else
    printf 'BILLSPLIT_PORT=%s\n' "$PORT" >> .env
  fi
  ok "Kept your existing .env (secret key preserved)"
fi

# --- 5. sanity check ---------------------------------------------------------
say "Checking the app starts"
BILLSPLIT_SKIP_BACKGROUND=1 ./.venv/bin/python -c 'import app.main' \
  || die "The app failed to import. Nothing was installed as a service."
ok "Imports cleanly"

# --- 6. systemd --------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
  need_sudo
  say "Installing the $SERVICE service"

  # Restart=always is what lets the app update itself: the updater pulls the new
  # code and exits, and systemd brings it straight back on the new version.
  $SUDO tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=Bill Splitter - split costs with your group
Documentation=https://github.com/$REPO
After=network-online.target
Wants=network-online.target
# These two live in [Unit], not [Service]: they moved in systemd 230, and a
# modern systemd silently ignores them under [Service] and falls back to its
# default of 5 restarts per 10s - which would let a brief failure loop exhaust
# the budget and leave the app down. 8 tries over 2 minutes rides out a slow
# network at boot while still giving up on genuinely broken code.
StartLimitBurst=8
StartLimitIntervalSec=120

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$INSTALL_DIR/.venv/bin/python -m app.main
Restart=always
RestartSec=3

# Modest hardening. ReadWritePaths is what the self-updater needs: it writes
# into the checkout (git) and into data/ (database + backups).
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectHome=no
ReadWritePaths=$INSTALL_DIR

[Install]
WantedBy=multi-user.target
EOF

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE" >/dev/null 2>&1
  $SUDO systemctl restart "$SERVICE"

  sleep 3
  if $SUDO systemctl is-active --quiet "$SERVICE"; then
    ok "Service running and enabled at boot"
  else
    printf '\n'
    warn "The service did not come up. Recent log:"
    $SUDO journalctl -u "$SERVICE" -n 25 --no-pager || true
    die "Install incomplete."
  fi
  SERVICE_INSTALLED=1
else
  warn "No systemd here, so no service was installed."
  info "Start it by hand with:  cd $INSTALL_DIR && ./.venv/bin/python -m app.main"
  SERVICE_INSTALLED=0
fi

# --- 7. done -----------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP" ] && IP="$(hostname 2>/dev/null || echo localhost)"

printf '\n%s\n' "${GREEN}${BOLD}Bill Splitter is installed.${OFF}"
printf '\n  %s  %s\n' "${BOLD}Open:${OFF}" "http://$IP:$PORT"
printf '  %s\n\n' "${DIM}The first account you create becomes the admin.${OFF}"

if [ "$SERVICE_INSTALLED" = "1" ]; then
  cat <<EOF
  ${BOLD}Handy commands${OFF}
    sudo systemctl status $SERVICE      status
    sudo systemctl restart $SERVICE     restart
    sudo journalctl -u $SERVICE -f      follow the log

EOF
fi

cat <<EOF
  ${BOLD}Where things live${OFF}
    $INSTALL_DIR/data/billsplit.db      your data
    $INSTALL_DIR/data/backups/          automatic snapshots
    $INSTALL_DIR/.env                   port, secret key

  ${BOLD}Next${OFF}
    1. Open the URL above and create the admin account.
    2. Approve anyone else who signs up, under Admin → Users.
    3. Point Admin → Updates at your GitHub repo to enable self-updating.

EOF
