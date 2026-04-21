# Frameworks Hermes Setup

Replicable infrastructure for running a [Hermes Agent](https://github.com/nous-research/hermes) as a reactive GitHub bot on the [security-alliance/frameworks](https://github.com/security-alliance/frameworks) repository.

Anyone can clone this, run `setup.sh`, and have their own reactive agent with their own bot identity, whitelist, and signing key -- all backed by the same relay + tunnel architecture.

---

## What you get

- A Hermes profile named **frameworks** with a reactive GitHub agent personality (customized to your bot identity during setup)
- A GitHub webhook relay that receives events and dispatches them to Hermes
- A Cloudflare tunnel to expose the relay to GitHub webhooks
- GPG-signed commits from your bot identity
- systemd user services for the relay and tunnel
- Three layers of review/comment dedup protection
- Dangerous command auto-deny with audit logging
- Stuck spawn detection with automatic rescue
- Model fallback chain for resilience

## Prerequisites

- **Hermes Agent** installed (`pip install hermes-agent` or from source)
- **GitHub account** for the bot with a classic PAT (repo, workflow, read:org scopes)
- **GPG key** for signing commits (create one or import an existing one during setup)
- **Cloudflare tunnel token** (if using cloudflared for tunneling)
- **Python 3.9+** with `aiohttp` and `pyyaml`
- **gh CLI** authenticated (`gh auth login`)
- **git** with GPG signing support

## Quick Start

```bash
git clone https://github.com/frameworks-volunteer/frameworks-hermes.git
cd frameworks-hermes
./setup.sh
```

The setup script walks you through 7 steps:

1. **Hermes profile** -- imports the `frameworks` profile skeleton and generates your SOUL.md from a template
2. **GPG key** -- selects or creates a signing key
3. **Git config** -- sets up your bot identity and signing
4. **Relay** -- symlinks relay code to `~/ops/frameworks-gh-relay/`, generates config.env from template
5. **Frameworks repo** -- clones with fork/origin and upstream remotes
6. **systemd services** -- installs relay + tunnel units
7. **Start** -- optionally starts services

Everything that is user-specific (bot name, GPG key, PAT, webhook secret, tunnel token, sender whitelist) is prompted during setup. Nothing is hardcoded.

---

## Architecture

```
GitHub webhook --> Cloudflare tunnel --> relay (port 9191)
       |                                      |
       |                              1. Verify signature
       |                              2. Dedupe (SQLite)
       |                              3. Check repository
       |                              4. Self-event filter
       |                              5. Whitelist check
       |                              6. Classify event
       |                              7. Choose model
       |                              8. Build prompt
       |                              9. Enqueue work
       |
       v
  Worker thread picks up work item
       |
       v
  Spawn Hermes (PTY) with profile + skills
       |
       v
  Monitor output:
    - Auto-deny dangerous commands
    - Track completed actions (dedup guard)
    - Detect stuck spawns
    - Kill-on-rescue (prevent race conditions)
       |
       v
  Agent uses gh CLI / git --> push to fork --> PR to upstream
```

---

## Directory layout

```
frameworks-hermes/
  setup.sh                    # Main setup script (7 steps)
  README.md                   # This file
  docs/
    opencode-worker-design.md # Future: OpenCode as relay worker (design doc)
  profile/
    frameworks.tar.gz         # Hermes profile skeleton (SOUL.md only, no identity)
    SOUL.md.template          # Agent personality template ({{BOT_USERNAME}}, {{ALLOWED_SENDERS}})
  relay/
    relay.py                  # Webhook relay server (~1175 lines)
    test_relay.sh             # Integration test (filter validation)
    config.env.template       # Config template (all secrets removed)
    .gitignore                # Excludes config.env, deliveries.db, spawns/, logs
  systemd/
    frameworks-gh-relay.service   # Relay service (paths: REPLACE_ME)
    cloudflared-frameworks.service # Tunnel service (token: CLOUDFLARE_TUNNEL_TOKEN)
```

---

## How it works

### Event filtering pipeline

Every incoming webhook goes through this pipeline before any Hermes spawn:

| Step | Check | Result if failed |
|------|-------|-----------------|
| 1 | HMAC signature verification | 403 |
| 2 | Delivery ID dedup (SQLite, 48h) | 200 "duplicate" |
| 3 | Repository must match ALLOWED_REPO | 200 "wrong repo" |
| 4 | Sender must not be BOT_USERNAME | 200 "self-event" |
| 5 | Sender must be in ALLOWED_SENDERS | 200 "not whitelisted" |
| 6 | Event type + action classification | 200 "not in scope" |
| 7 | Model selection + prompt building | -- |

If all checks pass, the relay responds with 202 "accepted" and enqueues the work item.

### Event types handled

| GitHub event | Action | Condition | Agent scope |
|---|---|---|---|
| issues | assigned | assignee == BOT_USERNAME | issue_assigned |
| pull_request | assigned | assignee == BOT_USERNAME | pr_assigned |
| pull_request | review_requested | requested_reviewer == BOT_USERNAME | pr_review_requested |
| issue_comment | created | body mentions @BOT_USERNAME or contains trigger phrases | issue_comment |
| pull_request_review | submitted | body mentions @BOT_USERNAME or contains trigger phrases | pr_review |
| pull_request_review_comment | created | body mentions @BOT_USERNAME or contains trigger phrases | pr_review_comment |

Trigger phrases for comments/reviews: "please fix", "please review", "please look", "take a look", "can you", "could you", "needs review", or the bot's username.

### Model fallback chain

The relay tries models in order from `MODEL_CHAIN` (config.env). If the primary model fails or gets rate-limited (429), it falls back to the next one automatically. Example chain:

```
openrouter/z-ai/glm-5.1 -> openrouter/minimax/MiniMax-M2.7 -> openrouter/kimi-coding-cn/kimi-k2.5
```

When the bot reviews its own PRs (self-review), it uses `SELF_REVIEW_MODELS` instead -- alternate models to avoid using the same model that wrote the code.

### Safety features

**Dangerous command auto-deny:**

The relay monitors the PTY output for Hermes's "DANGEROUS COMMAND:" prompts (the [o/s/D] choice that Hermes shows before executing shell commands). When one appears, the relay automatically sends `d` (deny) to the prompt within milliseconds. Denied commands are logged to `dangerous_cmds.log` for audit. This prevents the agent from running destructive commands like `rm -rf`, force pushes, etc.

**Stuck spawn detection:**

If a Hermes spawn produces no output for `STUCK_TIMEOUT` seconds (default 180), the relay:
1. Kills the stuck process immediately (prevents race conditions)
2. Spawns a rescue agent with a different model from the fallback chain
3. The rescue agent reads the stuck spawn's output log, diagnoses the issue, and either completes the task or leaves a comment explaining what happened

**Kill-on-rescue:**

The original spawn is killed BEFORE the rescue agent runs. This prevents both from operating simultaneously and submitting duplicate reviews/comments.

**Review/comment dedup (3 layers):**

1. **Prompt-level:** The agent's prompt instructs it to check for existing reviews before submitting, and to never submit a duplicate.
2. **Rescue prompt:** The rescue agent checks the original spawn's output for successful review/comment submissions. If found, it is told "DO NOT submit another review."
3. **PTY-level hard guard:** The relay tracks every `gh pr review`, `gh pr comment`, and `gh issue comment` call in a `completed_actions` set. If the agent attempts a second call of the same type in the same spawn, the relay kills the process immediately.

**GH body rule (--body-file heredoc):**

The agent is instructed to never use `--body` with inline text, because double quotes cause bash to expand backticks as command substitution (which caused a triple-review incident). Instead, the agent always uses:

```bash
cat > /tmp/${SPAWN_ID}_body.md << 'EOF'
(body content with backticks, dollar signs, etc.)
EOF
gh pr review NUM --approve --body-file /tmp/${SPAWN_ID}_body.md
```

The single-quoted `'EOF'` prevents ALL shell expansion. The `$SPAWN_ID` env var (unique per spawn) prevents filename collisions when multiple spawns run concurrently.

### Concurrency

The relay enforces `MAX_CONCURRENT` (default 3) parallel Hermes processes. If the queue is full, new work items wait. Queue depth is logged and a warning is emitted at depth >= 10.

---

## Configuration

All runtime configuration lives in `~/ops/frameworks-gh-relay/config.env`:

| Variable | Description | Default |
|---|---|---|
| GITHUB_WEBHOOK_SECRET | Secret from the GitHub webhook settings | (required) |
| GITHUB_TOKEN | Classic PAT with repo, workflow, read:org | (required) |
| ALLOWED_REPO | Repository full name | security-alliance/frameworks |
| BOT_USERNAME | Your bot's GitHub username | (required) |
| ALLOWED_SENDERS | Comma-separated whitelisted usernames | (required) |
| MODEL_CHAIN | Fallback chain: provider/model pairs | (see template) |
| SELF_REVIEW_MODELS | Models for reviewing bot's own PRs | (see template) |
| HERMES_BIN | Path to hermes binary | ~/.../hermes |
| REPO_PATH | Local path to the frameworks clone | ~/frameworks |
| RELAY_PORT | Port the relay listens on | 9191 |
| DELIVERY_DB | Path to dedup SQLite database | deliveries.db |
| LOG_FILE | Relay log path | relay.log |
| DANGEROUS_CMD_LOG | Denied commands audit log | dangerous_cmds.log |
| STUCK_TIMEOUT | Seconds with no output before rescue | 180 |
| MAX_CONCURRENT | Max parallel Hermes processes | 3 |
| MAX_SPAWN_SECONDS | Hard kill timeout per spawn | 900 |

See `relay/config.env.template` for the full list with descriptions.

---

## GPG key setup

The agent must sign all commits with a GPG key. You have two options:

**Option A: Create a new key during setup**

setup.sh will offer to list your existing keys. If you don't have one, create it:

```bash
gpg --full-generate-key
# Choose: RSA and RSA, 4096 bits, no expiration
# Use your bot's email: USERNAME@users.noreply.github.com
```

**Option B: Import an existing key**

If you're migrating from another machine, export the key first:

```bash
# On the source machine:
gpg --armor --export-secret-keys KEY_FINGERPRINT > my-bot-secret-key.asc
gpg --armor --export KEY_FINGERPRINT > my-bot-public-key.asc

# Transfer both files securely to the new machine, then:
gpg --import my-bot-public-key.asc
gpg --import my-bot-secret-key.asc
```

After import, verify the key is available:

```bash
gpg --list-secret-keys
gpg --sign --detach-sign --armor -u KEY_FINGERPRINT /dev/null
```

The key must have no passphrase (the agent runs non-interactively and cannot enter a passphrase).

---

## Registering the GitHub webhook

The relay needs a webhook registered on the upstream repository pointing to your tunnel URL. You need **admin** or **repo admin** permission on the upstream repository to register webhooks.

After setup.sh completes, it prints a curl command. Or register manually:

```bash
curl -X POST \
  -H "Authorization: token YOUR_GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/security-alliance/frameworks/hooks \
  -d '{
    "name": "web",
    "active": true,
    "events": ["issues","pull_request","issue_comment","pull_request_review","pull_request_review_comment"],
    "config": {
      "url": "https://YOUR_TUNNEL_URL/webhook",
      "content_type": "json",
      "secret": "YOUR_WEBHOOK_SECRET"
    }
  }'
```

Replace:
- `YOUR_GITHUB_TOKEN` -- classic PAT with repo admin scope
- `YOUR_TUNNEL_URL` -- your cloudflared tunnel URL (e.g. `fwks.example.com`)
- `YOUR_WEBHOOK_SECRET` -- the same value as `GITHUB_WEBHOOK_SECRET` in config.env

The webhook events to register: `issues`, `pull_request`, `issue_comment`, `pull_request_review`, `pull_request_review_comment`.

---

## Fork workflow

The agent uses a fork-based workflow:

```
origin   = YOUR_USERNAME/frameworks  (fork, push branches here)
upstream = security-alliance/frameworks (official, PRs and issues here)
```

- NEVER push to upstream. Always push branches to origin, then open PRs.
- ALL commits must be GPG-signed (`git commit -S`).
- PRs are created with: `gh pr create --repo security-alliance/frameworks --head USERNAME:BRANCH`
- The `develop` branch is the default base for all PRs.

---

## Skills loaded by the agent

The relay tells Hermes to load these skills for every spawn:

- **frameworks-reactive-github** -- core reactive agent procedures (event handling, behavior scopes, self-review policy)
- **github-auth** -- GitHub authentication setup (git credential handling)
- **github-issues** -- Create, manage, triage, and close GitHub issues
- **github-pr-workflow** -- Full pull request lifecycle (branch, commit, push, PR)
- **github-code-review** -- Review code changes, leave inline comments, submit reviews

These are bundled with Hermes. If you're adapting this setup for a different repository, you can change the `--skills` argument in the `spawn_hermes()` function in relay.py and the rescue agent's skill list in `spawn_rescue()`.

---

## Customizing the agent personality

After setup, edit `~/.hermes/profiles/frameworks/SOUL.md` to adjust the agent's behavior, tone, or rules. The template uses `{{BOT_USERNAME}}` and `{{ALLOWED_SENDERS}}` placeholders that setup.sh fills in, but you can change anything after the fact.

Key sections in SOUL.md:
- **IDENTITY** -- the bot's GitHub account and repository
- **CORE RULES** -- reactive-only, whitelist, self-ignore, mandatory prefix, concise
- **BEHAVIOR SCOPES** -- what events the agent handles and how
- **SKILLS** -- which Hermes skills to load

---

## Monitoring and debugging

### Check relay status

```bash
systemctl --user status frameworks-gh-relay
```

### Check tunnel status

```bash
systemctl --user status cloudflared-frameworks
```

### Read relay logs

```bash
# Live tail
tail -f ~/ops/frameworks-gh-relay/relay.log

# Recent activity
tail -100 ~/ops/frameworks-gh-relay/relay.log
```

### Read spawn output

Every Hermes spawn writes to `~/ops/frameworks-gh-relay/spawns/`:

```
spawns/
  20260420_185700_issue_assigned_prompt.txt     # Prompt sent to Hermes
  20260420_185700_issue_assigned_output.log     # Full PTY output
  20260420_190302_rescue_20260420_185700_...log  # Rescue agent output (if spawned)
```

Spawn files are named: `TIMESTAMP_SCOPE_prompt.txt` and `TIMESTAMP_SCOPE_output.log`.

### Check dangerous command denials

```bash
cat ~/ops/frameworks-gh-relay/dangerous_cmds.log
```

Each line shows: timestamp, spawn ID, and the denied command.

### Check delivery dedup database

```bash
sqlite3 ~/ops/frameworks-gh-relay/deliveries.db "SELECT * FROM deliveries ORDER BY ts DESC LIMIT 20;"
```

### Restart the relay

```bash
systemctl --user restart frameworks-gh-relay
```

Always restart after modifying `relay.py` or `config.env`.

---

## Troubleshooting

**Relay not receiving webhooks:**
- Check the tunnel is running: `systemctl --user status cloudflared-frameworks`
- Check the tunnel URL matches what's registered in the GitHub webhook settings
- Verify the webhook secret in config.env matches the GitHub webhook secret

**Agent not responding to events:**
- Check the relay log for "not whitelisted" or "self-event" messages
- Verify `ALLOWED_SENDERS` includes the sender's username (case-insensitive)
- Verify `BOT_USERNAME` matches the bot's actual GitHub username
- Check the spawn output logs for errors

**Triple / duplicate reviews:**
This should no longer happen with the three-layer dedup protection. If it does, check:
1. The PTY log for "DUPLICATE ... detected -- killing spawn" (layer 3)
2. The rescue agent's prompt for review dedup instructions (layer 2)
3. The agent's prompt for "Before submitting, CHECK for existing reviews" (layer 1)

**Agent stuck / not producing output:**
- Check `STUCK_TIMEOUT` (default 180s) -- some complex reviews may take longer
- Check the rescue agent's spawn output log
- The relay kills stuck spawns automatically and dispatches a rescue agent

**GPG signing fails:**
- Verify the key exists: `gpg --list-secret-keys FINGERPRINT`
- Verify no passphrase: `gpg --sign --detach-sign --armor -u FINGERPRINT /dev/null`
- Verify git config: `git config --local user.signingkey` in the frameworks repo

**Hermes not found:**
- Verify `hermes` is in PATH or set `HERMES_BIN` in config.env to the full path
- Check: `which hermes` or `hermes --version`

---

## Adapting for other repositories

This setup is specific to security-alliance/frameworks but the architecture is general. To adapt it:

1. **Change ALLOWED_REPO** in config.env to your target repository
2. **Update SOUL.md.template** -- change the repository name, default branch, and behavior scopes
3. **Update the fork workflow** in the relay's `build_prompt()` function -- change `security-alliance/frameworks` references and the PR creation command
4. **Update test_relay.sh** -- change the repository name in test payloads
5. **Register a new webhook** on the target repository
6. **Adjust the skills list** in `spawn_hermes()` and `spawn_rescue()` if your use case differs

The core relay logic (signature verification, dedup, whitelist, stuck detection, dangerous command deny, rescue, dedup guard) is repository-agnostic and does not need changes.

---

## License

Same as security-alliance/frameworks.
