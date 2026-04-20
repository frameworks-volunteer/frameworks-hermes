# Frameworks Hermes Setup

Replicable infrastructure for running a [Hermes Agent](https://github.com/nous-research/hermes) as a reactive GitHub bot on the [security-alliance/frameworks](https://github.com/security-alliance/frameworks) repository.

Anyone can clone this, run setup.sh, and have their own reactive agent with their own identity, whitelist, and signing key -- all backed by the same relay + tunnel architecture.

## What you get

- A Hermes profile named **frameworks** with a reactive GitHub agent personality (customized to your bot identity during setup)
- A GitHub webhook relay that receives events and dispatches them to Hermes
- A Cloudflare tunnel to expose the relay to GitHub webhooks
- GPG-signed commits from your bot identity
- systemd user services for the relay and tunnel

## Prerequisites

- Hermes Agent installed (`pip install hermes-agent` or from source)
- A GitHub account with a classic PAT (repo, workflow, read:org scopes)
- A GPG key for signing commits (or create one during setup)
- A Cloudflare tunnel token (if using cloudflared)
- Python 3.9+ with `aiohttp` and `pyyaml`

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

## Directory layout

```
frameworks-hermes/
├── setup.sh                    # Main setup script
├── profile/
│   ├── frameworks.tar.gz       # Hermes profile skeleton (no identity)
│   └── SOUL.md.template        # Agent personality template ({{BOT_USERNAME}}, {{ALLOWED_SENDERS}})
├── relay/
│   ├── relay.py                # Webhook relay server
│   ├── test_relay.sh           # Integration test
│   ├── config.env.template     # Config template (secrets removed)
│   └── .gitignore
├── systemd/
│   ├── frameworks-gh-relay.service
│   └── cloudflared-frameworks.service
└── README.md
```

## How it works

```
GitHub webhook → Cloudflare tunnel → relay (port 9191) → spawns Hermes →
  git operations → push to fork → PR to upstream
```

The relay filters events by:
- Repository (security-alliance/frameworks only)
- Sender whitelist (you choose who can trigger actions)
- Event type (issues, pull_request, comments, reviews)
- Assignment/review-request to your bot

## Configuration

All runtime configuration lives in `~/ops/frameworks-gh-relay/config.env`:

| Variable | Description |
|---|---|
| GITHUB_WEBHOOK_SECRET | Secret from the GitHub webhook settings |
| GITHUB_TOKEN | Classic PAT with repo, workflow, read:org |
| ALLOWED_REPO | Repository full name (security-alliance/frameworks) |
| BOT_USERNAME | Your bot's GitHub username |
| ALLOWED_SENDERS | Comma-separated whitelisted usernames |
| MODEL_CHAIN | Fallback chain for Hermes models |
| SELF_REVIEW_MODELS | Models used when reviewing own PRs |
| RELAY_PORT | Port the relay listens on |
| REPO_PATH | Local path to the frameworks clone |

See `relay/config.env.template` for the full list.

## Customizing the agent personality

After setup, edit `~/.hermes/profiles/frameworks/SOUL.md` to adjust the agent's behavior, tone, or rules. The template uses `{{BOT_USERNAME}}` and `{{ALLOWED_SENDERS}}` placeholders that setup.sh fills in, but you can change anything after the fact.

## License

Same as security-alliance/frameworks.
