#!/usr/bin/env bash
# frameworks-hermes setup script
# Configures a Hermes profile + GitHub webhook relay for security-alliance/frameworks
#
# Usage: ./setup.sh [--non-interactive]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NON_INTERACTIVE=false
[[ "${1:-}" == "--non-interactive" ]] && NON_INTERACTIVE=true

# ---------- helpers ----------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

ask() {
  local prompt="$1" var="$2" default="${3:-}"
  if $NON_INTERACTIVE; then
    eval "$var=\"\${default:-}\""
    return
  fi
  if [[ -n "$default" ]]; then
    printf "%s [%s]: " "$prompt" "$default"
  else
    printf "%s: " "$prompt"
  fi
  read -r answer
  eval "$var=\"\${answer:-\$default}\""
}

ask_required() {
  local prompt="$1" var="$2"
  while true; do
    ask "$prompt" "$var"
    if [[ -n "${!var}" ]]; then
      break
    fi
    error "This value is required."
  done
}

sed_replace() {
  # Portable in-place sed replace: sed_replace FILE OLD NEW
  local file="$1" old="$2" new="$3"
  if [[ "$(uname)" == "Darwin" ]]; then
    sed -i '' "s|${old}|${new}|g" "$file"
  else
    sed -i "s|${old}|${new}|g" "$file"
  fi
}

# ---------- preflight ----------

info "Running preflight checks..."

MISSING=()
for cmd in hermes git gh python3; do
  if ! command -v "$cmd" &>/dev/null; then
    MISSING+=("$cmd")
  fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  error "Missing required commands: ${MISSING[*]}"
  error "Install them before running setup."
  exit 1
fi

# ---------- configuration ----------

info "Gathering configuration..."

ask "Your system username" USER_NAME "$(whoami)"
ask_required "Bot GitHub username" BOT_USERNAME
ask "Upstream repository" UPSTREAM_REPO "security-alliance/frameworks"
ask "Fork repository (your bot's fork)" FORK_REPO "${BOT_USERNAME}/frameworks"
ask "Whitelisted senders (comma-separated)" ALLOWED_SENDERS ""
if [[ -z "$ALLOWED_SENDERS" ]]; then
  ask_required "Whitelisted senders (comma-separated)" ALLOWED_SENDERS
fi

ask "GitHub webhook secret (or press Enter to generate one)" GITHUB_WEBHOOK_SECRET ""
if [[ -z "$GITHUB_WEBHOOK_SECRET" ]]; then
  GITHUB_WEBHOOK_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(20))')"
  info "Generated webhook secret: ${GITHUB_WEBHOOK_SECRET}"
  warn "Save this -- you will need it when creating the GitHub webhook."
fi

ask_required "GitHub Personal Access Token (classic, repo+workflow+read:org)" GITHUB_TOKEN

# ---------- Hermes profile ----------

info "Step 1/7: Importing Hermes 'frameworks' profile..."

if hermes profile list 2>/dev/null | grep -q "frameworks"; then
  warn "Profile 'frameworks' already exists. Skipping import."
else
  hermes profile import "$SCRIPT_DIR/profile/frameworks.tar.gz" --name frameworks
  info "Profile 'frameworks' imported."
fi

# Generate SOUL.md from template with user's values
SOUL_PATH="$HOME/.hermes/profiles/frameworks/SOUL.md"
if [[ -f "$SCRIPT_DIR/profile/SOUL.md.template" ]]; then
  sed -e "s|{{BOT_USERNAME}}|${BOT_USERNAME}|g" \
      -e "s|{{ALLOWED_SENDERS}}|${ALLOWED_SENDERS}|g" \
      "$SCRIPT_DIR/profile/SOUL.md.template" > "$SOUL_PATH"
  info "SOUL.md generated for ${BOT_USERNAME} with senders: ${ALLOWED_SENDERS}"
else
  warn "No SOUL.md.template found. Profile will use the placeholder."
fi

# ---------- GPG key ----------

info "Step 2/7: Configuring GPG signing key..."

ask "GPG signing key ID (fingerprint, or press Enter to list keys)" GPG_KEY_ID ""

if [[ -z "$GPG_KEY_ID" ]]; then
  echo ""
  info "Available GPG secret keys:"
  gpg --list-secret-keys --keyid-format=long 2>/dev/null || true
  echo ""
  ask_required "Enter the key fingerprint to use for signing" GPG_KEY_ID
fi

# Verify the key exists
if ! gpg --list-secret-keys "$GPG_KEY_ID" &>/dev/null; then
  error "GPG key $GPG_KEY_ID not found in your keyring."
  error "Import it first: gpg --import <keyfile>"
  exit 1
fi

# Get email from key for git config
GPG_EMAIL="$(gpg --list-keys --with-colons "$GPG_KEY_ID" 2>/dev/null | grep '^uid' | head -1 | cut -d'<' -f2 | cut -d'>' -f1)"
if [[ -z "$GPG_EMAIL" ]]; then
  ask_required "Email address for git commits" GPG_EMAIL
fi

# ---------- Git config ----------

info "Step 3/7: Configuring git..."

# Global settings
git config --global user.name "$BOT_USERNAME"
git config --global user.email "$GPG_EMAIL"
git config --global user.signingkey "$GPG_KEY_ID"
git config --global commit.gpgsign true
git config --global tag.gpgsign true
info "Global git config updated."

# ---------- Relay setup ----------

info "Step 4/7: Setting up webhook relay..."

OPS_DIR="$HOME/ops"
RELAY_DIR="$OPS_DIR/frameworks-gh-relay"
mkdir -p "$OPS_DIR"

if [[ -d "$RELAY_DIR" ]]; then
  warn "$RELAY_DIR already exists."
  ask "Replace with symlink to repo? (y/N)" REPLACE_RELAY "n"
  if [[ "$REPLACE_RELAY" =~ ^[Yy] ]]; then
    rm -rf "$RELAY_DIR"
  fi
fi

if [[ ! -d "$RELAY_DIR" ]]; then
  ln -s "$SCRIPT_DIR/relay" "$RELAY_DIR"
  info "Symlinked $SCRIPT_DIR/relay -> $RELAY_DIR"
fi

# Generate config.env from template
if [[ -f "$RELAY_DIR/config.env" ]]; then
  warn "config.env already exists. Not overwriting."
else
  cp "$SCRIPT_DIR/relay/config.env.template" "$RELAY_DIR/config.env"
  # Replace placeholders
  sed_replace "$RELAY_DIR/config.env" "REPLACE_ME" "$USER_NAME"
  sed_replace "$RELAY_DIR/config.env" "<REQUIRED>" "" # clear placeholders
  # Now set the actual values
  sed_replace "$RELAY_DIR/config.env" "GITHUB_WEBHOOK_SECRET=" "GITHUB_WEBHOOK_SECRET=$GITHUB_WEBHOOK_SECRET"
  sed_replace "$RELAY_DIR/config.env" "GITHUB_TOKEN=" "GITHUB_TOKEN=$GITHUB_TOKEN"
  sed_replace "$RELAY_DIR/config.env" "BOT_USERNAME=" "BOT_USERNAME=$BOT_USERNAME"
  sed_replace "$RELAY_DIR/config.env" "ALLOWED_SENDERS=" "ALLOWED_SENDERS=$ALLOWED_SENDERS"
  chmod 600 "$RELAY_DIR/config.env"
  info "config.env generated with your values."
fi

# ---------- Frameworks repo clone ----------

info "Step 5/7: Cloning frameworks repository..."

FW_DIR="$HOME/frameworks"

if [[ -d "$FW_DIR" ]]; then
  warn "$FW_DIR already exists. Skipping clone."
else
  git clone "https://github.com/${FORK_REPO}.git" "$FW_DIR"
  cd "$FW_DIR"
  git remote add upstream "https://github.com/${UPSTREAM_REPO}.git"
  git fetch upstream
  git checkout develop 2>/dev/null || git checkout -b develop upstream/develop
  # Local git config for signing
  git config --local user.name "$BOT_USERNAME"
  git config --local user.email "$GPG_EMAIL"
  git config --local user.signingkey "$GPG_KEY_ID"
  # Exclude .worktrees
  echo ".worktrees/" >> .git/info/exclude
  info "Frameworks repo cloned and configured."
fi

# ---------- systemd services ----------

info "Step 6/7: Installing systemd services..."

SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

for svc in frameworks-gh-relay cloudflared-frameworks; do
  if [[ -f "$SYSTEMD_DIR/$svc.service" ]]; then
    warn "$svc.service already exists. Not overwriting."
  else
    cp "$SCRIPT_DIR/systemd/$svc.service" "$SYSTEMD_DIR/$svc.service"
    sed_replace "$SYSTEMD_DIR/$svc.service" "REPLACE_ME" "$USER_NAME"
    info "Installed $svc.service"
  fi
done

# Cloudflare tunnel token
ask "Cloudflare tunnel token (or press Enter to skip)" CLOUDFLARE_TOKEN ""
if [[ -n "$CLOUDFLARE_TOKEN" ]]; then
  sed_replace "$SYSTEMD_DIR/cloudflared-frameworks.service" "CLOUDFLARE_TUNNEL_TOKEN" "$CLOUDFLARE_TOKEN"
fi

systemctl --user daemon-reload

# ---------- Start services ----------

info "Step 7/7: Starting services..."

ask "Start the relay service now? (Y/n)" START_RELAY "y"
if [[ "$START_RELAY" =~ ^[Yy] ]]; then
  systemctl --user enable frameworks-gh-relay.service
  systemctl --user start frameworks-gh-relay.service
  info "Relay service started on port 9191."
fi

if [[ -n "$CLOUDFLARE_TOKEN" ]]; then
  ask "Start the Cloudflare tunnel now? (Y/n)" START_TUNNEL "y"
  if [[ "$START_TUNNEL" =~ ^[Yy] ]]; then
    systemctl --user enable cloudflared-frameworks.service
    systemctl --user start cloudflared-frameworks.service
    info "Cloudflare tunnel started."
  fi
fi

# ---------- Summary ----------

echo ""
echo "=========================================="
info "Setup complete!"
echo "=========================================="
echo ""
echo "  Profile:     frameworks (hermes profile use frameworks)"
echo "  Relay:       $RELAY_DIR (port 9191)"
echo "  Repo:        $FW_DIR"
echo "  GPG key:     $GPG_KEY_ID"
echo ""

if [[ -n "$CLOUDFLARE_TOKEN" ]]; then
  TUNNEL_URL="$(systemctl --user show cloudflared-frameworks -p ActiveState &>/dev/null && echo 'active' || echo 'not started')"
  echo "  Tunnel:      $TUNNEL_URL"
else
  echo "  Tunnel:      NOT CONFIGURED (set token in systemd service)"
fi

echo ""
warn "IMPORTANT: Review and edit config.env if needed:"
echo "  $RELAY_DIR/config.env"
echo ""

# Print webhook registration curl command
echo "To register the GitHub webhook, run:"
echo ""
echo "  curl -X POST \\"
echo "    -H 'Authorization: token YOUR_GITHUB_TOKEN' \\"
echo "    -H 'Accept: application/vnd.github.v3+json' \\"
echo "    https://api.github.com/repos/${UPSTREAM_REPO}/hooks \\"
echo "    -d '{"
echo "      \"name\": \"web\","
echo "      \"active\": true\","
echo "      \"events\": [\"issues\",\"pull_request\",\"issue_comment\",\"pull_request_review\",\"pull_request_review_comment\"],"
echo "      \"config\": {"
echo "        \"url\": \"https://YOUR_TUNNEL_URL/webhook\","
echo "        \"content_type\": \"json\","
echo "        \"secret\": \"${GITHUB_WEBHOOK_SECRET}\""
echo "      }"
echo "    }'"
echo ""
info "Run 'frameworks chat' to start the agent."
