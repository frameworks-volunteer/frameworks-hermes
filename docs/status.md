# Frameworks Reactive Agent -- Status

Last updated: 2026-06-25

## Infrastructure

| Component         | Status  | Notes                                           |
|-------------------|---------|-------------------------------------------------|
| Relay service     | RUNNING | systemd user service, uptime 11+ days           |
| Cloudflare tunnel  | RUNNING | Token-based, remotely managed                   |
| Relay filter tests| PASS     | ping, wrong-repo, whitelist, self-event         |
| GitHub PAT        | VALID   | Classic (ghp_), scopes: repo, workflow, read:org |
| GPG signing       | OK      | Key 6B786ECD0A29B032DD345946997D13F278693F39     |

## Model Configuration

Primary chain (MODEL_CHAIN):
  1. openrouter/z-ai/glm-5.2 (primary)
  2. openrouter/moonshotai/kimi-k2.6 (fallback 1)
  3. openrouter/deepseek/deepseek-v4-flash (fallback 2)

Self-review chain (SELF_REVIEW_MODELS):
  1. openrouter/moonshotai/kimi-k2.6
  2. openrouter/deepseek/deepseek-v4-flash

## Toolset Configuration

Sub-agent spawns use --toolsets flag with explicit whitelist:
  terminal,file,search,web,skills,session_search,todo,delegation,vision,image_gen,code_exec,github

Excluded:
  - browser (prevents prompt injection via web pages)
  - clarify (prevents waiting for input in autonomous one-shot spawns)

## Event Types Handled

| Event type                  | Scope                | Trigger                        |
|-----------------------------|----------------------|--------------------------------|
| issues (assigned)           | issue_assigned       | assignee == bot                |
| issues (unassigned)         | issue_unassigned     | cancellation                   |
| pull_request (assigned)     | pr_assigned          | assignee == bot                |
| pull_request (unassigned)   | pr_unassigned        | cancellation                   |
| pull_request (review_req)   | pr_review_requested  | requested_reviewer == bot      |
| pull_request (review_removed)| pr_review_removed   | cancellation                   |
| issue_comment               | issue_comment/pr_comment | @mention or explicit request |
| pull_request_review         | pr_review            | @mention or explicit request   |
| pull_request_review_comment | pr_review_comment    | @mention or explicit request   |
| discussion                  | discussion           | @mention or explicit request   |
| discussion_comment          | discussion_comment   | @mention or explicit request   |

## Files (all OUTSIDE hermes-agent -- safe from updates)

  ~/ops/frameworks-gh-relay/relay.py          -- webhook relay (Python, PTY)
  ~/ops/frameworks-gh-relay/config.env        -- relay config
  ~/.hermes/SOUL.md                           -- agent identity
  ~/.hermes/skills/github/frameworks-reactive-github/SKILL.md -- procedures
  ~/repos/frameworks-hermes/                  -- backup repo (this repo)

## Known Issues

1. PAT expiry (2026-05-18 to 2026-06-25): All GitHub PATs expired causing
   missed reviews/comments. PAT regenerated on 2026-06-25. The relay's
   memory note about expired PATs is now stale.

2. "Can you" keyword trigger too broad: issue_comment and review events
   trigger on "can you" in the body, but this matches comments not addressed
   to the bot (e.g., PR #529 comment from scode2277 to DicksonWu654).
   Consider requiring @mention only, or narrowing keywords.

3. Relay needs restart after code changes: The running relay still has old
   code in memory. Run: systemctl --user restart frameworks-gh-relay.service
