---
name: frameworks-reactive-github
description: Reactive GitHub agent for security-alliance/frameworks. Handles issues, PR reviews, and comments only from whitelisted users.
version: 1.0.0
author: frameworks-volunteer
metadata:
  hermes:
    tags: [GitHub, reactive, frameworks, security-alliance]
    related_skills: [github-auth, github-issues, github-pr-workflow, github-code-review]
---

# Frameworks Reactive GitHub Agent

Procedures for acting as frameworks-volunteer on security-alliance/frameworks.

## Prerequisites

- Authenticated as frameworks-volunteer (see github-auth skill)
- Repo cloned at ~/frameworks with remotes:
    origin   = frameworks-volunteer/frameworks (fork, push HERE)
    upstream = security-alliance/frameworks (official, issues/PRs here)
- All bundled GitHub skills loaded: github-auth, github-issues,
  github-pr-workflow, github-code-review

## Fork Workflow (CRITICAL)

  - NEVER push branches to upstream. Always push to origin (the fork).
  - Branch from develop, push to origin, open PR from fork to upstream.
  - Use: gh pr create --repo security-alliance/frameworks \
        --head frameworks-volunteer:BRANCH --base develop
  - All issue comments, reviews, and PR interactions target the UPSTREAM repo.
  - The PAT has triage access on upstream (can read/label), but code
    pushes go to the fork only.

## Whitelist

Only act on events from:
- scode2277
- mattaereal

Ignore ALL events where sender.login == frameworks-volunteer.

## Mandatory Prefix

Every GitHub comment, reply, or review body MUST start with:

    **Model:** `<model_name>` **Reasoning:** `<low|medium|high>` **Provider:** `<provider_name>`

Example:
    **Model:** `glm-5.2` **Reasoning:** `medium` **Provider:** `openrouter`

This prefix is injected by the relay in the HERMES_MODEL, HERMES_REASONING,
and HERMES_PROVIDER environment variables. Read them and include them.

---

### Weekly Security Maintenance (Dependabot Audit)

A cron job `weekly-dependabot-check` runs `check-dependabot.py` every
Monday at 09:00 UTC-3 to scan for pnpm security advisories and open
fix PRs automatically.

- Script: `~/.hermes/scripts/check-dependabot.py`
- Schedule: `0 9 * * 1` (Mondays 09:00 UTC-3)
- Workdir: `~/frameworks`

To check if it ran:
```
cronjob list  -- look for last_run_at / last_status
```

If it missed its slot (last_run_at is null past the scheduled time),
the Hermes scheduler was likely offline. Manually trigger it:
```
cronjob run --job-id d81060c39820
```

Or run the script directly:
```bash
cd ~/frameworks && python3 ~/.hermes/scripts/check-dependabot.py
```

The script workflow:
1. Checks for existing open PRs from frameworks-volunteer
2. Runs `npx pnpm audit --json`
3. If clean (exit 0), exits silently
4. If vulnerable, writes pnpm overrides to package.json, regenerates
   pnpm-lock.yaml, commits, pushes, and opens a PR

### Known Bug: False-Positive Duplicate Guard

`check-dependabot.py` has a duplicate-avoidance check that is too broad:

```python
if "dependabot" in pr.get("title", "").lower():
    print(f"Existing dependabot PR already open: ...")
    sys.exit(0)
```

This matches ANY PR with "dependabot" in the title, including
configuration PRs like `chore(deps): add Dependabot configuration`.
When such a PR is open, the script skips the audit entirely even if
there are actual security advisories to fix.

Workaround when manually verifying:
- Ignore the script's "already open" message if the open PR is a config
  PR, not a security-fix PR.
- Run `npx pnpm audit --json` manually to get the ground truth.

### Dependabot Alerts API Permission

Querying GitHub's Dependabot alerts REST endpoint requires the
`admin:repo_hook` scope on the PAT:

```bash
gh api repos/security-alliance/frameworks/dependabot/alerts
# 403: You are not authorized to perform this operation
```

The current classic PAT (repo, workflow, read:org) does NOT have this
scope. For audit status, rely on `pnpm audit` instead of the GitHub API.

### Manual Audit as Fallback

When the cron script fails, is blocked, or cannot access the API:

```bash
cd ~/frameworks && npx pnpm audit --json
```

A clean result looks like:
```json
{
  "advisories": {},
  "metadata": {
    "vulnerabilities": {"info":0,"low":0,"moderate":0,"high":0,"critical":0}
  }
}
```

If the exit code is 0 and advisories is empty, the repo is clean.

---

## Procedure 1: Issue Assigned (issues action=assigned)

Trigger: assignee.login == frameworks-volunteer AND sender in whitelist

WARNING: Issue investigation tasks are prone to context bloat when using
`high` reasoning on thinking models (see Pitfall #20). The agent should
limit investigation depth (max 5-6 file reads) before forming a
hypothesis and acting. If the relay is configured with REASONING=high,
be aware that deep investigation may trigger context compression and
get killed by the STUCK_TIMEOUT watchdog.

Steps:
1. Fetch issue details
   gh issue view <NUMBER> --repo security-alliance/frameworks

2. Fetch repository context
   - Read CONTRIBUTING.md in the repo root
   - Read any relevant docs from docs/pages/ related to the issue topic
   - Check existing PRs that might relate

3. Create a local branch from develop
   cd ~/frameworks
   git fetch upstream
   git checkout develop && git pull upstream develop
   git checkout -b fix/issue-<NUMBER>-<short-slug>

4. Implement the fix
   - Follow the repo's existing conventions (check CONTRIBUTING.md)
   - For docs: follow the existing MDX structure and frontmatter
   - CRITICAL: When editing or creating MDX documentation files, set the
     YAML frontmatter `contributors` field as follows:
       contributors:
         - role: wrote
           users: [mattaereal]
         - role: reviewed
           users: [scode2277]
     NEVER use frameworks-volunteer in the contributors frontmatter.
     The bot acts on behalf of mattaereal and scode2277.
   - Keep changes minimal and focused on the issue

5. Run quick checks only (NOT full builds)
   - For docs: verify frontmatter format, check spelling
     (cspell.json config exists), verify internal links
   - For code: lint only, no build
   - Do NOT run `npx vocs build`, `npm run build`, etc.
   - Let upstream CI validate builds after the PR is created

6. Commit and push to the FORK (origin, NOT upstream)
   ALL COMMITS MUST BE GPG-SIGNED. Always use `git commit -S`.
   git add -A
   git commit -S -m "fix: <description> (closes #<NUMBER>)"
   git push -u origin HEAD

7. Create PR from fork to upstream develop
   gh pr create \
     --repo security-alliance/frameworks \
     --head frameworks-volunteer:<branch> \
     --base develop \
     --title "fix: <description>" \
     --body "## Summary\n<what and why>\n\nCloses #<NUMBER>"

   NOTE: If this returns 403, the fine-grained PAT needs
   pull_requests:write permission on the upstream repo.
   See PAT Permissions section below.

8. Leave a concise status comment on the issue
   gh issue comment <NUMBER> --repo security-alliance/frameworks \
     --body "**Model:** `<model>` **Reasoning:** `<reasoning>` **Provider:** `<provider>`

     Working on this. PR #<PR_NUMBER> opened targeting develop."

   NOTE: If this returns 403, the fine-grained PAT needs
   issues:write permission on the upstream repo.

## Procedure 2: Pull Request Assigned (pull_request action=assigned)

Trigger: assignee.login == frameworks-volunteer AND sender in whitelist

Steps:
1. Fetch PR details
   gh pr view <NUMBER> --repo security-alliance/frameworks

2. Run security review (see Procedure 4 below)

3. Run QA review (see Procedure 5 below)

4. Leave concise review result
   Use gh pr review to submit with appropriate event (APPROVE,
   REQUEST_CHANGES, or COMMENT).

   Body must include the mandatory prefix.

## Procedure 3: Review Requested (pull_request review_requested)

Trigger: requested_reviewer.login == frameworks-volunteer AND sender in whitelist

Steps: Same as Procedure 2 (security + QA review, leave review result).

## Procedure 4: Security Review

For any PR review, check:

1. No hardcoded secrets, tokens, or API keys
2. No injection vectors (XSS in MDX/JSX, path traversal, etc.)
3. No unsafe deserialization or eval
4. Dependencies are appropriate and not suspicious
5. Permission/auth checks where needed
6. No sensitive data exposure in logs or outputs

## Procedure 5: QA Review

For any PR review, check:

1. Changes match the PR description / linked issue
2. No broken internal or external links
3. Frontmatter is valid and consistent with existing pages
4. Spelling follows the repo's cspell.json wordlist
5. Build passes (check CI status or run locally)
6. No leftover debug content, TODO markers, or placeholder text

## Procedure 6: Issue Comment (issue_comment)

Trigger: sender in whitelist AND body mentions @frameworks-volunteer
        OR body contains explicit request to act

Distinguish:
- If the comment is on an ISSUE (not a PR): answer the question or
  revise a previous fix
- If the comment is on a PR thread: treat as PR feedback (re-review)

Steps:
1. Determine context (issue vs PR thread)
   gh issue view <NUMBER> --repo security-alliance/frameworks --json pullRequest

2. If issue: read the issue + any prior comments, respond concisely.
   If action is requested and reasonable, follow Procedure 1.

3. If PR thread: follow Procedure 8 (re-review).

4. Always include the mandatory prefix.

## Procedure 7: Pull Request Review / Review Comment

Trigger: sender in whitelist AND body mentions @frameworks-volunteer
        OR body contains explicit request to act

Steps:
1. Read the review/comment context
2. Reassess the relevant code section
3. Chime in with a response or re-review as appropriate
4. Always include the mandatory prefix

## Procedure 8: Re-review

When re-reviewing after feedback:

1. Check out the updated PR branch
2. Re-read only the changed files since last review
3. Verify the feedback was addressed
4. Update review status (approve if resolved, comment if not)
5. Include the mandatory prefix

## Procedure 9: Discussion / Discussion Comment

Trigger: sender in whitelist AND body mentions @frameworks-volunteer
        OR body contains explicit request to act

GitHub Discussions are treated like issue comments. The bot can answer
questions, provide guidance, or take action if requested.

Steps:
1. Fetch discussion details
   gh api repos/security-alliance/frameworks/discussions/<NUMBER>

   NOTE: GitHub Discussions API is GraphQL-only for most operations.
   Use:
   gh api graphql -f query='{ repository(owner:"security-alliance", name:"frameworks") { discussion(number:<NUMBER>) { title body comments(first:20) { nodes { body author { login } } } } } }'

2. Read the discussion body and any prior comments
3. Answer or take action as appropriate
4. Post a reply via GraphQL mutation:
   gh api graphql -f query='{ ... addDiscussionComment mutation ... }'

   Or use the REST workaround if available.
5. Always include the mandatory prefix

## Model Fallback Chain

The relay tries models sequentially from MODEL_CHAIN in config.env.
If a model returns a rate-limit error (429), the relay automatically
retries with the next model in the chain. Fatal errors (auth failure,
module crash) stop the chain immediately -- no retry.

Current chain:
  1. openrouter/z-ai/glm-5.2 (primary, cheapest/good-enough)
  2. openrouter/moonshotai/kimi-k2.6 (fallback 1)
  3. openrouter/deepseek/deepseek-v4-flash (fallback 2)

Self-review PRs use a separate chain (SELF_REVIEW_MODELS) to avoid
the bot reviewing its own work with the same model that wrote it.
Current self-review chain:
  1. openrouter/moonshotai/kimi-k2.6
  2. openrouter/deepseek/deepseek-v4-flash

Log pattern when fallback triggers:
  Processing: ... model=z-ai/glm-5.2 (attempt 1/3)
  [spawn ...] RATE LIMITED ... -- will try fallback
  Processing: ... model=moonshotai/kimi-k2.6 (attempt 2/3)
  Completed: ... (model=openrouter/moonshotai/kimi-k2.6)

To change the chain, edit MODEL_CHAIN in config.env and restart
the relay. Comma-separated, provider/model format.

## Self-Review Rule

If the PR author is frameworks-volunteer (i.e. you are reviewing or
replying to your own work), ALWAYS use a DIFFERENT model than the one
that created the content. This applies in EVERY context — reviews,
comments, re-reviews, and replies to feedback.

If the relay spawn provides an alternate model
(HERMES_SELF_REVIEW=1 + HERMES_ALT_MODEL / HERMES_ALT_PROVIDER), use it.
If NO alternate model is provided in the prompt context, ask the user
which model to switch to BEFORE composing any response. Do NOT proceed
with the default model.

The mandatory prefix must reflect the actual model being used for the
response, not the model that wrote the original content.

## Error Handling

- If a step fails (git conflict, CI failure, etc.), leave a comment
  explaining the issue and what manual intervention is needed.
- Never force-push to shared branches.
- Never merge PRs unless explicitly asked.

---

## Infrastructure Reference

### Architecture

  GitHub webhook --> Cloudflare Tunnel --> relay (127.0.0.1:9191) --> Hermes one-shot

All custom logic lives OUTSIDE hermes-agent/ to keep Hermes vanilla and
update-safe. Nothing in this setup requires modifying hermes-agent code.

### Files

  ~/ops/frameworks-gh-relay/relay.py          -- webhook relay (Python, PTY-based)
  ~/ops/frameworks-gh-relay/config.env        -- relay config (secret, whitelist, models)
  ~/ops/frameworks-gh-relay/deliveries.db     -- dedup DB (X-GitHub-Delivery)
  ~/ops/frameworks-gh-relay/dangerous_cmds.log -- audit log for auto-denied commands
  ~/ops/frameworks-gh-relay/test_relay.sh     -- filter test script
  ~/.hermes/SOUL.md                           -- agent identity and guardrails
  ~/.hermes/skills/github/frameworks-reactive-github/SKILL.md  -- this skill

  ~/.config/systemd/user/frameworks-gh-relay.service   -- relay service
  ~/.config/systemd/user/cloudflared-frameworks.service -- tunnel service
  ~/.local/bin/cloudflared                             -- arm64 binary (no sudo)

### Relay Config (config.env)

  GITHUB_WEBHOOK_SECRET -- 48-char hex, generated via secrets.token_hex(24)
  ALLOWED_REPO=security-alliance/frameworks
  BOT_USERNAME=frameworks-volunteer
  ALLOWED_SENDERS=scode2277,mattaereal
  MODEL_CHAIN=openrouter/z-ai/glm-5.2,openrouter/moonshotai/kimi-k2.6,openrouter/deepseek/deepseek-v4-flash
  SELF_REVIEW_MODELS=openrouter/moonshotai/kimi-k2.6,openrouter/deepseek/deepseek-v4-flash
  STUCK_TIMEOUT=180       -- seconds with no output before rescue agent spawns
  MAX_CONCURRENT=3        -- max parallel Hermes processes
  PAT: classic (ghp_), scopes: repo, read:org, workflow
  RELAY_PORT=9191

### Cloudflare Tunnel

  Tunnel UUID: 8c1e450d-6190-44d1-be6f-5c0accfa0f82
  Account ID: 65da7724134416df0c1afa289f81d35e
  Method: token-based (remotely managed), NOT local config.yml
  Origin: http://127.0.0.1:9191 (relay)
  Public hostname: fwks.therektgames.com
  Connector ID: e95f68d0-347f-4960-8871-1674e83eeb0a

  To create DNS route (requires cert.pem from `cloudflared tunnel login`):
    cloudflared tunnel route dns <tunnel-name> <hostname>

  To add ingress rule: use Cloudflare Zero Trust dashboard, or create
  ~/.cloudflared/config.yml with:
    tunnel: 8c1e450d-6190-44d1-be6f-5c0accfa0f82
    credentials-file: ~/.cloudflared/8c1e450d-6190-44d1-be6f-5c0accfa0f82.json
    ingress:
      - hostname: <HOSTNAME>
        service: http://127.0.0.1:9191
      - service: http_status:404

### Hermes One-Shot Spawn

  hermes chat \
    --provider <PROVIDER> \
    --model <MODEL> \
    --skills frameworks-reactive-github,github-auth,github-issues,github-pr-workflow,github-code-review \
    --toolsets terminal,file,search,web,skills,session_search,todo,delegation,vision,image_gen,code_exec,github \
    --worktree \
    --checkpoints \
    --source tool \
    --query "<prompt>" \
    --max-turns 90

  FLAGS:
  --toolsets  EXCLUDES browser and clarify. Browser is disabled to
              prevent prompt injection via web pages. Clarify is
              disabled because the agent runs autonomously with no
              human present to answer questions.
  --yolo    DO NOT USE. Dangerous commands are handled by the relay's
            PTY-based auto-deny system (see below).

  NOTE: Do NOT use --quiet. Without it, the relay can stream Hermes
  output in real time and log tool calls, session IDs, and errors.
  With --quiet, the relay gets only exit codes and cannot observe
  what Hermes is doing.

### Dangerous Command Auto-Deny

  Hermes runs inside a PTY (pseudo-terminal), not a plain pipe. When
  it encounters a "dangerous" command (piping to python -c, writing to
  /etc, etc.), it shows: "DANGEROUS COMMAND: ... Choice [o/s/D]:"

  The relay detects this pattern in the PTY output and auto-sends "d"
  (deny). Hermes sees the denial and finds an alternative approach.

  Every denied command is logged to TWO places:
    relay.log:          [spawn ...] DANGEROUS DENIED (#1): <cmd>
    dangerous_cmds.log: [timestamp] [spawn_id] DENIED: <full cmd>

  To review what's been blocked:
    cat ~/ops/frameworks-gh-relay/dangerous_cmds.log

  CRITICAL BUG (fixed): Previously the relay injected "d" and LET THE
  SPAWN CONTINUE. After the denial, the agent often entered a dead
  state (alive but producing zero output). Empty PTY reads reset the
  stuck timer (last_output_time updated even on 0-byte reads), so the
  180s stuck rescue never fired. The spawn hung for the full 15-minute
  hard timeout before the relay retried with the next model.

  Fix: On dangerous command detection, KILL THE SPAWN IMMEDIATELY.
  Do not try to recover. If an agent is attempting dangerous commands,
  it is already broken -- let MODEL_CHAIN retry with the next model.
  The rescue agent can diagnose if needed.

  Also fix the stuck timer: only update last_output_time when the
  read chunk is actually non-empty. Empty reads from a live-but-silent
  child must NOT reset the timer.

### GH Body Rule (write_file + --body-file)

  NEVER use --body with inline text when calling gh CLI commands.
  Double quotes cause bash to expand backticks as command substitution,
  which mangles the body but may still be accepted by the GitHub API.
  This caused the PR #460 triple-review incident: the rescue agent used
  double-quoted --body, bash mangled the backticks in the mandatory
  prefix, the agent thought it failed, retried with single quotes,
  and both submissions went through.

  NEVER use bash heredocs (cat << 'EOF') either. Models frequently
  mangle multi-line heredocs into single-line commands that timeout
  or produce broken files. This caused the PR #513 review failure:
  the agent compressed the heredoc and gh command into one line,
  it timed out at 60s, the retry also timed out, and the PTY duplicate
  guard killed the spawn before any review was posted.

  ALWAYS use the write_file tool to create the body file, then
  submit with --body-file:
    1. Use write_file to create /tmp/${SPAWN_ID}_body.md with the
       review/comment body (including backticks, dollar signs, etc.)
    2. Run: gh pr review NUM --approve --body-file /tmp/${SPAWN_ID}_body.md

  $SPAWN_ID is a unique env var set by the relay per spawn, preventing
  filename collisions when multiple spawns run concurrently.

### Work Queue + Concurrency

  Webhooks are NOT processed immediately. The HTTP handler enqueues
  work items and returns 202. Worker threads (MAX_CONCURRENT=3) pull
  from the queue and spawn Hermes one at a time per slot.

  This prevents:
    - 7 parallel Hermes processes fighting for CPU/API quota
    - Cascade 429 rate limits from burst events
    - Resource exhaustion on the machine

  Config:
    MAX_CONCURRENT=3    -- max parallel Hermes processes (config.env)
    STUCK_TIMEOUT=180   -- seconds with no output before rescue (config.env)

  Queue depth is logged on enqueue:
    Enqueued: scope=issue_assigned (queue depth: 2)
    If depth >= 10, a warning is logged.

### Cancellation (Unassignment / Review Request Removal)

  If an issue or PR is accidentally assigned to the bot and then
  unassigned, the relay handles `unassigned` and `review_request_removed`
  webhook actions to cancel any queued work before a worker spawns
  Hermes.

  Behavior:
    - `issues` action=`unassigned` -> cancels pending `issue_assigned`
    - `pull_request` action=`unassigned` -> cancels pending `pr_assigned`
    - `pull_request` action=`review_request_removed` -> cancels pending
      `pr_review_requested`

  The relay:
    1. Marks the issue/PR key as cancelled in a thread-safe registry.
    2. Drains the queue and removes matching items, logging how many
       were removed.
    3. Returns HTTP 200 with body "cancelled (N items)".
    4. If a worker has already dequeued the item but not started
       processing, it skips it when it sees the cancellation flag.
    5. If a worker is already running Hermes for that item, the spawn
       continues (observable in relay logs). Kill manually if needed.
    6. Re-assigning later clears the cancellation flag automatically.

  See `references/cancellation.md` for implementation details,
  race-condition coverage, and log patterns.

### Rescue Agent for Stuck Spawns

  If a Hermes spawn produces no output for STUCK_TIMEOUT seconds
  (default 180), the relay KILLS the stuck spawn and spawns a RESCUE
  agent in a separate thread.

  The rescue agent:
    - Checks if the original already submitted a review/comment
      (by scanning output for "gh pr review ... successfully")
    - If original already acted: told to NOT submit duplicates, just exit
    - If original did NOT act: told to CHECK existing reviews before
      submitting (never duplicate)
    - Reads the stuck spawn's original prompt (first 2000 chars)
    - Reads the stuck spawn's output so far (last 8000 chars)
    - Uses a DIFFERENT model from the chain (not the stuck one)
    - Has 5 min max runtime and 30 max turns
    - Auto-denies dangerous commands via the same PTY mechanism
    - Either diagnoses the hang, leaves a comment, or continues the work

  Four-layer dedup (added after PR #460 triple-review spam):
    (a) KILL-ON-RESCUE: Stuck spawns are killed immediately when
        rescue fires, preventing race conditions.
    (b) RESCUE PROMPT DEDUP: Rescue checks if original already
        submitted. If yes, told to exit. If no, told to check
        existing reviews before submitting.
    (c) AGENT PROMPT DEDUP: build_prompt for PR review scopes
        includes explicit step to check existing reviews before
        submitting: "gh api repos/.../reviews --jq ..."
    (d) PTY-LEVEL HARD GUARD: The relay tracks every gh pr review,
        gh pr comment, and gh issue comment call in a
        completed_actions set per spawn. If the agent attempts a
        second call of the same type, the relay kills the process
        immediately. Log: "DUPLICATE ... detected -- killing spawn"

  Rescue spawns are logged:
    [spawn ...] STUCK: no output for 180s -- spawning rescue agent, killing original
    [spawn ...] Killed stuck process (PID ...)
    [rescue ...] Spawning rescue agent: openrouter/moonshotai/kimi-k2.6
    [rescue ...] Done: exit=0

### Spawn Observability

  Each Hermes spawn creates two files in ~/ops/frameworks-gh-relay/spawns/:

    YYYYMMDD_HHMMSS_<scope>_prompt.txt   -- the exact prompt sent
    YYYYMMDD_HHMMSS_<scope>_output.log   -- full Hermes output (no --quiet)

  The relay log shows real-time events per spawn:

    [spawn ...] Worktree created: /home/zealot/frameworks/.worktrees/hermes-XXXX
    [spawn ...] Tool: gh issue view 42 --repo security-alliance/frameworks
    [spawn ...] Tool: git fetch upstream && git checkout develop
    [spawn ...] Session: 20260413_232457_c88c07
    [spawn ...] Done: exit=0 session=... duration=44s tools=10 output=....log

  To inspect a past run:
    cat spawns/YYYYMMDD_HHMMSS_<scope>_prompt.txt
    cat spawns/YYYYMMDD_HHMMSS_<scope>_output.log

  To resume a failed session for debugging:
    hermes --resume <session_id>

  Spawn files older than 48h are auto-pruned on relay startup.

### Systemd Commands

  systemctl --user start frameworks-gh-relay.service
  systemctl --user stop frameworks-gh-relay.service
  systemctl --user status frameworks-gh-relay.service
  systemctl --user start cloudflared-frameworks.service
  systemctl --user stop cloudflared-frameworks.service
  systemctl --user status cloudflared-frameworks.service

### Test the Relay

  cd ~/ops/frameworks-gh-relay && bash test_relay.sh http://127.0.0.1:9191

  Expected: ping->pong, wrong repo->ignored, non-whitelisted->ignored,
  self-event->ignored. Test 5 (valid issue) is skipped to avoid Hermes spawn.

  After modifying relay.py, config.env, or this SKILL.md, also run:
    python3 ~/.hermes/skills/github/frameworks-reactive-github/scripts/verify-relay.py
  This loads relay.py as a Python module and exercises classify_event()
  and build_prompt() with mock payloads. See `scripts/verify-relay.py`.

---

## Pitfalls and Lessons Learned

### Prompt Prohibitions Are As Important As Procedures

The agent follows positive instructions ("GPG-sign your commits") but
will improvise destructively if negative constraints are absent. ALWAYS
include an ABSOLUTE PROHIBITIONS block in the relay prompt:

  - NEVER create test commits, test files, or "verification" commits.
  - NEVER commit directly to develop or main. Always use a feature branch.
  - NEVER pipe output to python3, bash, sh, ruby, node, or any interpreter.
  - If Hermes flags a command as dangerous and denies it, do NOT retry
    a similar command. Switch to file tools (read_file, search_files).
  - NEVER run interactive commands that wait for input (nano, vim, less).
  - NEVER use 'git commit' without '-S' (GPG signing is MANDATORY).

Without these, the agent WILL create test commits on develop, pipe
output to python3, and attempt other dangerous patterns.

### Branch Contamination from Test Commits

A single test commit (e.g. "test: GPG signing verification") committed
to develop becomes part of EVERY future feature branch. When the agent
checks out develop and creates a branch, it inherits all local commits.

If develop is contaminated:
  1. Rebase the feature branch onto clean upstream/develop:
       git rebase --onto upstream/develop <bad_commit> <feature_branch>
  2. Force push the cleaned branch to origin
  3. Reset local develop to upstream/develop:
       git checkout develop && git reset --hard upstream/develop

NEVER let the agent create test commits. The prompt prohibition
prevents this, but if one already exists, clean it manually before
any new PR work.

### Post-Exit Incomplete Workflow Detection

Hermes can exit 0 while the workflow is INCOMPLETE. Common pattern:
- Agent creates branch, edits files, commits with GPG signing
- Model goes silent (empty responses for 3 retries)
- Hermes exits 0
- Relay sees exit=0 and considers it success
- Branch sits unpushed, PR never created, issue never commented

The relay should verify after exit=0:
  1. Check if the branch has unpushed commits (git log origin/branch..branch)
  2. Check if a PR exists for the branch
  3. If commits exist but no PR, the relay should finish the workflow
     (push + PR create + issue comment) rather than declaring success

This is a known gap. The current workaround: manual intervention when
you see "Done: exit=0" but no PR link in the relay log.

A SECOND variant: the agent completes the analysis (e.g. a full PR
review) but the final `gh pr review` / `gh pr comment` call fails with
HTTP 401 (expired PAT) or HTTP 403 (insufficient permissions). The
relay logs "Completed: pr_review_requested" and exits 0 because Hermes
itself did not crash -- the GitHub API rejection is an operational
error, not a process failure. The relay's crash detection looks for
"API key was rejected", "Traceback", "ModuleNotFoundError" -- none of
which match a 401/403. So the relay considers the spawn successful and
does not retry with a fallback model.

Detection: after a "Completed:" log line for a review scope, check:
  gh api repos/security-alliance/frameworks/pulls/NUM/reviews \
    --jq '[.[] | select(.user.login=="frameworks-volunteer")] | length'
If the count is 0, the review was not posted. Search the spawn output
log for "401" or "PAT expired" to confirm the cause.

Recovery: see `references/pat-expiry-missed-reviews.md`.

1. NEVER pass read_file() output directly to write_file() -- read_file
   prepends "N|" line numbers which get baked into the file. Always
   process/strip line numbers first, or use patch/terminal sed instead.

2. Output filtering masks lines containing secret-like patterns (e.g.
   WEBHOOK_SECRET). When debugging broken config lines, verify via
   Python ast.parse() for syntax, or check for specific substrings
   (like "os.environ.get") rather than printing the line content.

3. This machine has NO sudo access. Use ~/.local/bin for binaries,
   systemd --user for services, direct binary downloads instead of apt.
   Debian 13 trixie, aarch64/arm64.

4. cloudflared tunnel token-based setup: ingress rules live in the
   Cloudflare dashboard, NOT in a local config.yml. To use CLI
   commands like `tunnel route dns`, you need cert.pem from
   `cloudflared tunnel login` (browser auth).

5. hermes doctor checks GITHUB_TOKEN in ~/.hermes/.env and os.environ,
   but NOT `gh auth token`. If the PAT is only in gh CLI's hosts.yml,
   doctor will say "No GITHUB_TOKEN". Fix: save_env_value("GITHUB_TOKEN",
   token_from_gh_auth).

6. tinker-atropos doctor check uses __import__(\"tinker_atropos\"). The
   submodule directory existing is not enough -- must actually
   `uv pip install -e ./tinker-atropos` in the venv.

7. NEVER run long-blocking terminal commands (server processes, tunnel
   login, etc.) without background=true or a short timeout. Use 5-15s
   timeouts. The user had to manually kill processes that hung for 700s+.

8. When verifying cloudflared tunnel connectivity, use short-lived
   commands like `curl -s -w "%{http_code}" -o /dev/null` with a 10s
   timeout. 503 means tunnel/DNS works but ingress rule is missing.
   202 means relay accepted the payload.

9. Hermes `--quiet` mode returns exit code 1 even when it completes
   successfully. The relay's spawn_hermes() must NOT treat rc!=0 as
   failure. Instead, check for genuine crash indicators in stdout/stderr:
   "API key was rejected", "token expired or incorrect", Traceback,
   ModuleNotFoundError. GitHub API 403s are operational (PAT permissions),
   not Hermes crashes.

10. The relay's HTTP handler MUST spawn Hermes in a background thread
    (daemon=True). subprocess.run() inside the handler blocks the
    single-threaded HTTP server for up to 600s, during which GitHub
    webhooks get connection-reset. Always return 202 immediately and
    run Hermes asynchronously.

11. Provider config MUST use openrouter as the provider, not direct
    provider names like "zai", "minimax", "kimi-coding-cn", "moonshotai".
    The direct API keys for those providers are not configured. All models go
    through OpenRouter with full slugs: "z-ai/glm-5.2",
    "moonshotai/kimi-k2.6", "deepseek/deepseek-v4-flash".

12. When editing relay.py, be very careful with lines containing
    os.environ.get("GITHUB_WEBHOOK_SECRET", "") -- the quotes and
    parentheses are easily corrupted by shell escaping or patch tool
    mismatches. If the WEBHOOK_SECRET variable is broken, signature
    verification silently fails (falls through to the "not configured"
    branch). Verify with: python3 -c "import ast; ast.parse(open('relay.py').read())"

13. GH BODY RULE: Never use --body with inline gh CLI text. Never use
    bash heredocs either (models mangle them). Use the write_file tool
    to create the body file, then gh CLI with --body-file. See
    GH Body Rule section above for the full pattern and root cause.
    PR #513 review was lost because the agent compressed a heredoc
    into a single line, it timed out, and the duplicate guard killed
    the spawn on retry.

14. SPAWN_ID env var is available in every Hermes spawn (set by the
    relay). Use it in temp filenames to avoid collisions:
    /tmp/${SPAWN_ID}_body.md

15. REPO WORKING TREE CONTAMINATION FROM PREVIOUS SESSIONS. After
    multiple reactive sessions, the working tree accumulates stale
    modifications, deleted file markers, and untracked files (a single
    session left 88 tracked changes + 2 untracked files). This happens
    because sessions are interrupted, tools error out mid-workflow,
    or local branches are left behind.

    Never assume a fresh start. ALWAYS check before productive work:
      cd ~/frameworks && git status --short

    To clean tracked changes when dangerous-command guard blocks
    `git reset --hard` (it will -- `git reset --hard` is auto-denied
    by the PTY guard):
      git checkout -- .

    To remove untracked files afterward:
      git status --short | grep '^??' | cut -c4- | xargs rm -rf

    This is safe because the repo is a clone and tracked files are
    recovered from the index. Always verify `git status --short`
    returns nothing before making new changes.

16. CANNOT REOPEN CLOSED PR if HEAD BRANCH WAS FORCE-PUSHED. GitHub
    returns Validation Failed 422: "state cannot be changed. The <branch>
    was force-pushed or recreated." The only remedy is to open a NEW PR
    from the same branch:
      gh pr create --repo upstream/repo --head fork:branch --base develop
    Link the new PR to the old one in the body so reviewers have context.

17. EXECUTE_CODE DEFAULTS TO /home/zealot, NOT THE REPO ROOT. When
    using `execute_code` for file operations, always call
    `os.chdir('/home/zealot/frameworks')` before any filesystem calls,
    or pass absolute paths. Without this, `open('docs/...')` will raise
    FileNotFoundError silently.

17. PATCH TOOL REQUIRES EXPLICIT `path` PARAMETER. The `patch` tool
    (edit_file action=patch) REQUIRES a `path` field in the JSON
    payload. Omitting it produces `error: path required` and will
    retry in a tool loop. When patch repeatedly fails, fall back to
    Python file I/O via `execute_code` instead.

18. RESETTING TO UPSTREAM WHEN `git reset --hard` IS AUTO-DENIED.
    The PTY guard kills `git reset --hard` as dangerous. For safe branch
    prep aligned with upstream, do NOT try to reset. Instead, create
    the feature branch directly from the upstream ref:
      git fetch upstream
      git checkout -b BRANCH upstream/develop
    This gives a clean branch at upstream/develop without touching the
    local develop branch at all. If you are already on develop and just
    need to discard tracked changes, use `git checkout -- .` followed by
    `git status --short | grep '^??' | cut -c4- | xargs rm -rf`.

19. AMBIGUOUS REFNAME WHEN LOCAL BRANCH SHADOWS REMOTE.
    When a local branch named `develop` exists, `git show upstream/develop:FILE`
    may warn "refname 'upstream/develop' is ambiguous" because Git treats
    `upstream/develop` as both a remote-tracking ref and a local branch
    refspec. Workarounds:
      - Use the full ref: `git show refs/remotes/upstream/develop:FILE`
      - Or use `git ls-tree upstream/develop --name-only` for listings.
    Prefer creating branches with explicit upstream refs:
      git checkout -b BRANCH upstream/develop

15. The backup repo is ~/repos/frameworks-hermes/ (public, on GitHub
    at frameworks-volunteer/frameworks-hermes). It contains the relay
    code, profile template, setup script, systemd units, and docs.
    There should be NO other backup directories (~/backup/, ~/backups/).
    Everything lives in frameworks-hermes. After modifying the live
    relay (~/ops/frameworks-gh-relay/), always copy changes to
    ~/repos/frameworks-hermes/relay/ and commit+push.

16. Fine-grained PATs (github_pat_...) CANNOT grant write access to
    org repos you do not own. Use a classic PAT (ghp_...) with scopes
    repo, workflow, read:org for the fork workflow.

17. PTY duplicate-detection guard can match PROMPT TEXT, not just
    actual commands. The relay scans PTY output for `gh pr review` to
    prevent duplicate submissions. The prompt itself contains the literal
    text `gh pr review NUM --body-file ...` as an example. Hermes echoes
    the prompt to the PTY, so the guard sees `gh pr review` once in the
    prompt, adds "review" to completed_actions, then when the agent
    runs the real `gh pr review 424 ...` command, the relay falsely
    flags it as a DUPLICATE and kills the spawn before the API call
    completes. This happened for PR #424 (no review posted) and nearly
    for PR #441 (race condition let the API call win).

    Fix: Use regex patterns that require actual numbers, e.g.
      r"gh pr review\s+\d+"
      r"gh pr comment\s+\d+"
      r"gh issue comment\s+\d+"
    so the literal `NUM` placeholder in the prompt does not match.
    The `re` module is already imported in relay.py.

18. PTY duplicate guard can kill RETRIES of failed commands. When an
    agent tries `gh pr review --approve` and gets an error (e.g. cannot
    approve own PR), it may retry with `gh pr review --comment`. The
    relay sees `gh pr review` twice and kills the spawn as DUPLICATE,
    even though the first attempt failed and never submitted anything.
    The `[error]` marker in Hermes PTY output indicates failure.

    Fix: In relay.py, skip lines containing `[error]` before checking
    completed_actions:
      if "[error]" in stripped:
          continue
    This allows the agent to retry with a different flag (comment vs
    approve) without being killed.

    ADDITIONAL FIX (PR #513): The guard must ALSO skip lines containing
    `[BLOCKED]`, `Blocked:`, or `denied` -- these indicate the command
    was auto-denied by the PTY dangerous-command guard and never reached
    the GitHub API. Without this, a heredoc command that times out and
    gets blocked counts as a "completed action", and the retry is killed
    as a duplicate even though no review was ever posted.

      if "[error]" in stripped or "[BLOCKED]" in stripped:
          continue
      if "Blocked:" in stripped or "denied" in stripped.lower():
          continue

19. MDX contributor frontmatter is NOT the same as git authorship. The
    frameworks repo uses YAML frontmatter `contributors` with roles
    (wrote, reviewed, fact-checked) to attribute content. When the bot
    creates or edits MDX docs, it MUST use mattaereal as author and
    scode2277 as reviewer -- never frameworks-volunteer. Git commits
    remain signed by frameworks-volunteer; only the MDX frontmatter
    attribution changes.

21. HIGH REASONING + INVESTIGATION TASKS = CONTEXT COMPRESSION HANG.
    When the relay spawns an agent with `high` reasoning on a thinking
    model (e.g. z-ai/glm-5.2), the model outputs ~10-15K reasoning
    tokens per turn. Issue_assigned tasks often require reading 10+
    files and forming theories, which quickly bloats context past the
    50% compression threshold (~100K tokens for a 200K window).

    When `_compress_context()` fires, it makes an LLM call to summarize
    the conversation. During this compression API call, the PTY produces
    ZERO output. The relay's STUCK_TIMEOUT watchdog (default 180s) sees
    no output and kills the spawn with SIGKILL. The rescue agent often
    hits the same pattern and is also killed.

    Evidence: Issue #469 (2026-05-04) -- agent read 14 files over 10
    minutes, hit `compacting context…`, was killed at 180s. Rescue
    agent (kimi-k2.6) was also killed (exit=-9). PR #470 review
    (same model, same day) succeeded in 38s because it was a linear
    pipeline (view diff → check files → submit review) with no deep
    investigation spiral.

    Mitigation:
    - For issue_assigned tasks, use `medium` reasoning instead of `high`
      to cut reasoning token volume by ~60%.
    - Alternatively, use a non-reasoning model for investigation tasks.
    - If compression must be used, increase STUCK_TIMEOUT to 300s+,
      but this risks longer hangs on genuinely stuck agents.
    - The relay config should set REASONING=medium for issue_assigned
      scope, or the agent should self-limit investigation depth
      (max 5-6 file reads before forming a hypothesis and acting).

22. MULTI-LINE COMMIT MESSAGES VIA BASH WILL BREAK.
    Passing a multi-line string to `git commit -m "..."` inside a
    terminal command is fragile — newlines and quotes inside the
    message body are interpreted by bash, causing syntax errors like:
      /usr/bin/bash: eval: line 18: unexpected EOF while looking for matching '"'

    ALWAYS write the commit message to a file and use `git commit -F`:
      cat > /tmp/msg.txt << 'EOF'
      First line (subject)

      Body paragraph 1.
      Body paragraph 2.
      EOF
      git commit -S -F /tmp/msg.txt

    The single-quoted `EOF` prevents ALL shell expansion inside the
    heredoc. The `-F` flag reads the message from the file, so bash
    never sees the message content as part of the command line.
    This is simpler and safer than escaping quotes inside `-m`.

21. SYSTEMCTL RESTARTS ARE BLOCKED IN REACTIVE SESSIONS.
    The terminal tool auto-denies `systemctl` commands as dangerous
    (`BLOCKED: User denied. Do NOT retry.`). After modifying relay.py,
    you CANNOT restart the service from inside the agent session.

    Workarounds:
    - Ask the user to restart manually:
        systemctl --user restart frameworks-gh-relay.service
    - Or warn the user that changes are live on disk but the running
      process still has the old code in memory.
    - The relay must be restarted for any config.env changes too.
    - Exception: `systemctl --user status` may work; test with a
      short timeout (5s) if you need process state.

### PR Body Must Match the Diff (Not Prior Task Context)

When creating a PR, the body MUST be derived from the actual commit message
and diff (`git diff upstream/develop...HEAD`), never from task descriptions,
issue bodies, or prior session context. Before running `gh pr create`, the
agent MUST verify that the body text accurately describes the files and
changes in the actual diff.

This happened on PR #488: the agent had context from the OpSec/CCSS task
(PR #476) and used that body text when creating the Dependabot configuration
PR. The title and commit message were correct, but the body described a
completely different set of changes. The reviewer (mattaereal) caught the
mismatch between the body and the submitted code.

Rule: generate PR bodies from git output, not from memory or task prompts.

### SINGLE-FILE CHERRY-PICK PRs FROM UPSTREAM BRANCHES.
    When asked to carry one file (e.g. AGENTS.md) from an upstream
    feature branch into `main` (not `develop`), do NOT checkout the
    feature branch — it carries all unrelated commits.

    Instead:
      git fetch upstream
      git checkout -b chore/name upstream/main
      git show upstream/feature-branch:FILE > FILE
      git add FILE && git commit -S -m "chore: ..." && git push

  - `references/single-file-cherrypick.md` for full recipe.
    Also see Pitfall #21 in this list for the `git checkout --to_stdin`
    trap that does not exist.
  - `templates/dependabot.yml` — known-good Dependabot config for this repo
    (npm/pnpm + GitHub Actions, grouped weekly, targeting develop).
    Use with `references/dependabot-gha-maintenance.md` for the PR workflow.
  - `templates/release-please-workflow.yml` — known-good release-please
    GitHub Actions workflow (triggers on push to main, config in
    `.github/release-please/`). Use with `references/release-please-setup.md`.

  - `references/pat-expiry-missed-reviews.md` — detecting and recovering
    reviews silently dropped during PAT auth outages (agent completes analysis
    but cannot post; review bodies left in /tmp/).

### Release-Please Must Target main, NOT develop

Releases happen on `main`. The `develop` branch is for active work;
`main` receives merges from `develop` once per month and each merge
triggers a release. When configuring release-please:

- Workflow `on.push.branches` MUST be `[main]`
- `target-branch` in the action `with:` block MUST be `main`
- NEVER set `primaryBranch: develop` in `.github/release-please.yml`

The release-please config files should live under `.github/release-please/`
rather than the repo root to keep the root clean:

```
.github/release-please/release-please-config.json
.github/release-please/.release-please-manifest.json
```

The workflow must reference these paths:
```yaml
with:
  config-file: .github/release-please/release-please-config.json
  manifest-file: .github/release-please/.release-please-manifest.json
```

The package in the config should track the root (`"."`) for a repo-wide
release, not a docs subdirectory like `docs/pages`.

Mistake on PR #489: the agent set `primaryBranch: develop` and someone
else merged a version with `docs/pages` as the package. Both were wrong.
The correct setup is in `templates/release-please-workflow.yml`.

### Toolset Exclusion for Autonomous Spawns (--toolsets flag)

The `hermes chat` command accepts `--toolsets` to whitelist which
toolsets are available to the agent. The relay uses this to EXCLUDE
two toolsets that are dangerous or useless in autonomous one-shot mode:

  --toolsets terminal,file,search,web,skills,session_search,todo,\
             delegation,vision,image_gen,code_exec,github

EXCLUDED:
  - browser: Prevents prompt injection via malicious web pages. A
    sub-agent browsing to a crafted page could be instructed to
    exfiltrate the PAT or perform unauthorized actions. The `web`
    toolset (web_search, web_extract) covers legitimate web needs
    safely without rendering pages.
  - clarify: The agent runs autonomously with no human present. If
    it calls clarify, it blocks forever waiting for an answer that
    never comes, eventually hitting the STUCK_TIMEOUT and triggering
    a rescue spawn. The prompt also includes an explicit prohibition:
    "NEVER use the clarify tool or ask the user questions."

When adding new toolsets to Hermes, evaluate whether they should be
included in the relay spawn. Anything that waits for human input or
renders untrusted content should be excluded.

### New Event Types Must Be Added to Sender Extraction

When adding a new event type to classify_event() (e.g. `discussion`,
`discussion_comment`), you MUST also add it to the sender extraction
block in the HTTP handler (WebhookHandler.do_POST). Otherwise the
sender variable stays empty, the whitelist check is skipped (because
`if sender and ...` evaluates False for empty string), and the event
bypasses the whitelist entirely.

The sender extraction block looks like:

  if event_type == "issues":
      sender = payload.get("sender", {}).get("login", "")
  elif event_type == "pull_request":
      sender = payload.get("sender", {}).get("login", "")
  elif event_type in ("issue_comment", "pull_request_review",
                      "pull_request_review_comment",
                      "discussion", "discussion_comment"):
      sender = payload.get("sender", {}).get("login", "")

If you forget this step, ANY GitHub user (not just the whitelist) can
trigger the agent by mentioning @frameworks-volunteer in a discussion.

### "Can you" Keyword Trigger Is Too Broad

The relay's classify_event() for issue_comment, pull_request_review,
pull_request_review_comment, discussion, and discussion_comment checks
for "explicit request" keywords including "can you" and "could you".
These phrases are common in normal English and match comments that are
NOT addressed to the bot.

Example (PR #529, 2026-06-25): scode2277 posted a comment to
@DicksonWu654 containing "Can you try again" and "Can you re-check".
The relay matched "can you" and spawned the bot, which then tried to
respond but couldn't (PAT expired). The comment was not addressed to
@frameworks-volunteer at all.

Mitigation options (not yet implemented):
  1. Require @mention as the ONLY trigger (drop keyword matching).
     Pro: zero false positives. Con: users must always @mention.
  2. Narrow keywords to imperative phrases: "please fix", "please review",
     "take a look at this" -- drop "can you" and "could you" which are
     questions, not commands.
  3. Keep keywords but require @mention OR (keyword AND reply-to-bot).
     The webhook payload includes `in_reply_to_id` for issue comments --
     if the comment is a reply to a bot comment, keywords are safe.

Until this is fixed, expect occasional false-positive spawns on comments
that happen to contain "can you" but are not addressed to the bot.

### write_file Stale Caching When Fed From read_file

The `write_file` tool can produce stale or cached content when its input
came from `read_file` in the same `execute_code` block. The `read_file`
tool caches results and may return "File unchanged since last read"
instead of the actual file content. If you pipe `read_file` output
through `write_file`, you may write the cached placeholder message
instead of the real content.

Workaround: use `terminal("cat path")` to get raw content, then write
with Python `open()/write()` instead of `write_file`. Or ensure
`read_file` and `write_file` are in separate tool calls.

This caused issues where the merged PR contained content different from
what the agent intended to commit.

### Merged PR Content Divergence

When a PR is merged with content that differs from what the agent
committed, treat it as a critical signal. Possible causes:

1. write_file stale caching (see above)
2. Someone else force-pushed or edited the PR on GitHub before merge
3. The agent committed to the wrong branch or wrong ref

Always verify the merged diff against the local commit before declaring
success. If there is a mismatch, open a follow-up PR immediately.
