# Frameworks Hermes Setup

Replicable Hermes Agent + GitHub webhook relay setup for the
[security-alliance/frameworks](https://github.com/security-alliance/frameworks)
repository.

## What this gives you

- A Hermes profile named **frameworks** with the reactive GitHub agent personality
- A GitHub webhook relay that receives events and dispatches them to Hermes
- A Cloudflare tunnel to expose the relay to GitHub webhooks
- GPG-signed commits from the bot identity
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

The setup script will guide you through:

1. **Hermes profile import** -- imports the `frameworks` profile
2. **Relay symlink** -- links relay code to `~/ops/frameworks-gh-relay/`
3. **GPG key** -- asks for your signing key (or helps create one)
4. **Git config** -- sets up bot identity and signing
5. **Relay config** -- generates `config.env` from the template
6. **Frameworks repo clone** -- clones with fork/origin and upstream remotes
7. **systemd services** -- installs and starts relay + tunnel
8. **GitHub webhook** -- prints the curl command to register the webhook

## Directory layout

```
frameworks-hermes/
├── setup.sh                    # Main setup script
├── profile/
│   └── frameworks.tar.gz       # Hermes profile archive
├── relay/
│   ├── relay.py                # Webhook relay server
│   ├── test_relay.sh           # Integration test
│   ├── config.env.template     # Config template (secrets removed)
│   └── .gitignore
├── systemd/
│   ├── frameworks-gh-relay.service
│   └── cloudflared-frameworks.service
├── hooks/                      # Hermes hooks (if any)
└── README.md
```

## Post-setup

After running setup.sh:

1. Edit `~/ops/frameworks-gh-relay/config.env` to fill in secrets
2. Start the relay: `systemctl --user start frameworks-gh-relay`
3. Start the tunnel: `systemctl --user start cloudflared-frameworks`
4. Register the webhook on GitHub (the setup script prints the curl command)
5. Run `frameworks chat` to start the agent

## Architecture

```
GitHub webhook → Cloudflare tunnel → relay (port 9191) → spawns Hermes →
  git operations → push to fork → PR to upstream
```

The relay filters events by:
- Repository (security-alliance/frameworks only)
- Sender whitelist (configured in config.env)
- Event type (issues, pull_request, comments, reviews)
- Assignment/review-request to the bot

## Configuration

All runtime configuration lives in `~/ops/frameworks-gh-relay/config.env`:

| Variable | Description |
|---|---|
| GITHUB_WEBHOOK_SECRET | Secret from the GitHub webhook settings |
| GITHUB_TOKEN | Classic PAT with repo, workflow, read:org |
| ALLOWED_REPO | Repository full name (security-alliance/frameworks) |
| BOT_USERNAME | Bot's GitHub username |
| ALLOWED_SENDERS | Comma-separated whitelisted usernames |
| MODEL_CHAIN | Fallback chain for Hermes models |
| SELF_REVIEW_MODELS | Models used when reviewing own PRs |
| RELAY_PORT | Port the relay listens on |
| REPO_PATH | Local path to the frameworks clone |

See `relay/config.env.template` for the full list.

## License

Same as security-alliance/frameworks.
