# Frameworks Reactive Agent -- Status

Last updated: 2026-08-30

## Infrastructure

| Component         | Status  | Notes                                           |
|-------------------|---------|-------------------------------------------------|
| Relay service     | RUNNING | needs restart after 2026-08-30 incomplete-workflow change |
| Cloudflare tunnel  | RUNNING | Token-based, remotely managed                   |
| Relay filter tests| PASS     | ping, wrong-repo, whitelist, self-event         |
| GitHub PAT        | VALID   | Classic (ghp_), scopes: repo, workflow, read:org |
| GPG signing       | OK      | Key 6B786ECD0A29B032DD345946997D13F278693F39     |

## Model Configuration

Primary chain (MODEL_CHAIN), reasoning=medium for ALL scopes:
  1. openrouter/x-ai/grok-4.5 (primary)
  2. openrouter/z-ai/glm-5.2 (fallback 1)
  3. openrouter/moonshotai/kimi-k2.6 (fallback 2)
  4. openrouter/deepseek/deepseek-v4-flash (fallback 3)

Self-review chain (SELF_REVIEW_MODELS), also medium:
  1. openrouter/moonshotai/kimi-k2.6
  2. openrouter/deepseek/deepseek-v4-flash

DO NOT use reasoning=high with grok-4.5. PR #616 (2026-08-30) hit
finish_reason=length mid-tool-call, exited 0, left unpushed commits.

## Post-exit incomplete workflow detection (2026-08-30)

After every non-fatal spawn, `_detect_incomplete_workflow()` checks:
  1. Output truncation markers (finish_reason=length, etc.)
  2. Unpushed commits in THIS spawn's worktree only
  3. Missing scope-required GitHub action (review/comment/PR)

Non-empty reasons → return None → MODEL_CHAIN fallback (same path as 429).

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

## Resolved Issues

- PR #616 (2026-08-30): grok-4.5 high truncation + exit-0 incomplete
  workflow. Fixed: medium reasoning + post-exit incomplete detection.

## Known Issues

1. "Can you" keyword trigger too broad: issue_comment and review events
   trigger on "can you" in the body, but this matches comments not addressed
   to the bot. Consider requiring @mention only, or narrowing keywords.

2. Relay needs restart after code changes: The running relay still has old
   code in memory. Run: systemctl --user restart frameworks-gh-relay.service

3. HTTP 401/403 on final gh call may still look "completed" to the PTY
   guard (command line seen). Incomplete detection does not cover this yet.
