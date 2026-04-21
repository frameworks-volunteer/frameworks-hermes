# OpenCode as Relay Worker -- Design Doc

Status: DRAFT / FUTURE TODO
Created: 2026-04-14
Context: https://opencode.ai, https://opencode.ai/docs/permissions/

## TL;DR

Replace or supplement Hermes one-shot spawns with `opencode run` for
code-editing tasks. OpenCode has native permission sandboxing, built-in
LSP, code-aware editing, and structured JSON output. It would eliminate
the PTY-based dangerous-command auto-deny hack and produce better code
edits.

## Current Architecture

  GitHub webhook --> Cloudflare Tunnel --> relay.py (port 9191)
         |
         +--> Worker thread --> spawn_hermes()
                                  |
                                  +--> subprocess.Popen("hermes chat ...")
                                       PTY-based, one-shot, 900s max
                                       Does EVERYTHING: read issue, plan,
                                       edit files, git commit, push, PR

Problems:
- Hermes is a general chat agent, not a code editor
- Uses terminal commands for everything (fragile on PTY)
- "Dumb terminal" errors when git tries to open nano during rebase
- git add -A picks up worktree dirs during rebase conflict resolution
- Dangerous command detection is regex on PTY output (fragile)
- No LSP support, no structured code editing

## Proposed Architecture

  GitHub webhook --> Cloudflare Tunnel --> relay.py (port 9191)
         |
         +--> Worker thread --> spawn_hermes()  [ORCHESTRATOR]
                                  |
                                  +--> Hermes reads issue, plans work,
                                       then delegates CODE CHANGES to:
                                       |
                                       +--> spawn_opencode()  [WORKER]
                                            |
                                            +--> opencode run "Fix the
                                                 incident-response template
                                                 validation per issue #323"
                                                 --model openrouter/z-ai/glm-5.1
                                                 --format json
                                                 --cwd ~/frameworks/.worktrees/oc-XXXX

Hermes handles: issue reading, planning, PR creation, comments,
review, git operations (commit, push, rebase).
OpenCode handles: file editing, code generation, LSP-assisted
refactoring, conflict resolution.

## Why OpenCode Is a Better Worker

### 1. Permission Sandboxing (the big win)

Your proposed config:

  {
    "$schema": "https://opencode.ai/config.json",
    "permission": {
      "bash": "ask",
      "edit": "allow",
      "webfetch": "deny",
      "mcp_*": "deny"
    }
  }

With granular bash rules:

  "bash": {
    "*": "ask",
    "git *": "allow",
    "gh *": "allow",
    "npm *": "deny",
    "rm *": "deny",
    "curl *": "deny"
  }

This replaces our entire PTY-based dangerous command auto-deny
system. OpenCode has this built-in natively. No more parsing
"DANGEROUS COMMAND:" prompts from PTY output.

Even better, we can do path-level edit restrictions:

  "edit": {
    "*": "allow",
    ".env": "deny",
    "config.env": "deny"
  }

And restrict git push targets:

  "bash": {
    "git push origin *": "allow",
    "git push upstream *": "deny",
    "git push * --force*": "deny"
  }

### 2. Built-in LSP

OpenCode can download and use language servers. For the frameworks
repo (MDX/TS), it would understand the AST, catch broken frontmatter,
validate internal links, etc. Hermes can't do any of this.

### 3. Code-Aware Editing

OpenCode uses structured edit operations (not raw terminal sed/echo).
It understands file syntax, can make targeted edits without breaking
formatting, and can undo cleanly with /undo. Hermes uses
write_file/patch which are text-level only.

### 4. Structured Output

`opencode run --format json` returns machine-parseable JSON events.
The relay can programmatically detect success/failure, extract changed
files, count token usage. Hermes output is unstructured text parsed
with regex.

## OpenCode CLI Integration Points

### One-shot mode (most useful for relay)

  opencode run "Fix the validation bug in incident-response templates"
    --model openrouter/z-ai/glm-5.1
    --format json
    --cwd ~/frameworks

Returns JSON events on stdout. No PTY needed. No interactive prompts.

### Environment variables (key for relay integration)

  OPENCODE_CONFIG_CONTENT - Inline JSON config (no file needed!)
  OPENCODE_PERMISSION     - Inline JSON permissions config
  OPENCODE_CONFIG_DIR     - Custom config directory

These mean the relay can inject per-spawn config without writing files.

### ACP mode (for deeper integration)

  opencode acp --cwd ~/frameworks

Starts an ACP (Agent Client Protocol) server via stdin/stdout using
nd-JSON. This is the same protocol Claude Code uses. Could enable
bidirectional communication between relay and worker.

### Server mode (for persistent workers)

  opencode serve --port 4096

Headless HTTP server. Multiple `opencode run --attach` calls can
reuse the same backend, avoiding MCP server cold boot on each spawn.

## Implementation Plan

### Phase 1: Install and Verify

  npm i -g opencode-ai@latest
  opencode auth login  # configure OpenRouter
  opencode run "Respond with exactly: OPENCODE_SMOKE_OK"

Verify:
- arm64 binary works
- OpenRouter models (glm-5.1, MiniMax-M2.7) work
- --format json produces parseable output
- Permission config is respected

### Phase 2: Create Project Config

~/frameworks/opencode.json:

  {
    "$schema": "https://opencode.ai/config.json",
    "permission": {
      "bash": {
        "*": "ask",
        "git *": "allow",
        "gh *": "allow",
        "git push origin *": "allow",
        "git push upstream *": "deny",
        "rm *": "deny",
        "npm *": "deny",
        "npx *": "deny",
        "curl *": "deny",
        "python* -c *": "deny"
      },
      "edit": "allow",
      "read": "allow",
      "glob": "allow",
      "grep": "allow",
      "list": "allow",
      "webfetch": "deny",
      "websearch": "deny",
      "codesearch": "allow",
      "lsp": "allow",
      "todowrite": "allow",
      "question": "deny",
      "doom_loop": "deny",
      "skill": "deny"
    },
    "model": "openrouter/z-ai/glm-5.1",
    "provider": {
      "openrouter": {
        "api": "https://openrouter.ai/api/v1",
        "env": ["OPENROUTER_API_KEY"]
      }
    }
  }

Note: This file can be .gitignored since we'll use
OPENCODE_PERMISSION env var for relay spawns anyway.

### Phase 3: Add spawn_opencode() to relay.py

  def spawn_opencode(prompt, provider, model, scope=""):
      """Spawn OpenCode as a one-shot worker."""
      spawn_id = time.strftime("%Y%m%d_%H%M%S")
      if scope:
          spawn_id += f"_{scope}"

      # Create a worktree for isolation
      worktree_dir = str(REPO_PATH / ".worktrees" / f"oc-{spawn_id[-6:]}")
      subprocess.run(
          ["git", "worktree", "add", worktree_dir, "HEAD"],
          cwd=REPO_PATH, capture_output=True
      )

      cmd = [
          OPENCODE_BIN, "run",
          "--model", f"{provider}/{model}",
          "--format", "json",
          "--cwd", worktree_dir,
          prompt,
      ]

      # For relay mode: allow bash, deny only catastrophic patterns
      inline_perms = json.dumps({
          "bash": {
              "*": "allow",
              "git push upstream *": "deny",
              "rm -rf /": "deny",
              "npm *": "deny",
          },
          "edit": "allow",
          "webfetch": "deny",
      })

      env = {
          **os.environ,
          "GIT_EDITOR": "true",
          "GIT_SEQUENCE_EDITOR": "true",
          "EDITOR": "true",
          "VISUAL": "true",
          "OPENCODE_PERMISSION": inline_perms,
      }

      proc = subprocess.Popen(
          cmd,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          text=True,
          cwd=worktree_dir,
          env=env,
      )

      # Parse JSON events from stdout line by line
      results = []
      for line in proc.stdout:
          try:
              event = json.loads(line)
              results.append(event)
              if event.get("type") == "tool_call":
                  log.info("[opencode %s] Tool: %s", spawn_id,
                           event.get("tool", "?"))
          except json.JSONDecodeError:
              pass  # non-JSON output

      proc.wait()
      return worktree_dir, results, proc.returncode

### Phase 4: Integration Modes

MODE A: OpenCode as pure worker, Hermes orchestrates

  Hermes reads the issue, plans the work, then outputs a delegation
  marker:

    DELEGATE_TO_OPENCODE: Fix the validation in docs/pages/incident-response/

  The relay detects this pattern in Hermes's output, pauses Hermes,
  spawns OpenCode, feeds the result back to Hermes. Hermes then
  handles commit/push/PR.

  Pros: Best of both worlds (Hermes reasoning + OpenCode editing).
  Cons: Complex relay logic, two agents per task, higher latency.

MODE B: OpenCode replaces Hermes entirely for issue-assigned tasks

  For issue-assigned events, the relay spawns OpenCode directly
  with a detailed system prompt containing the same constraints
  (fork workflow, GPG signing, etc.).

  Pros: Simpler, single agent, no Hermes dependency for code tasks.
  Cons: Loses Hermes's skill system, session resume, reasoning depth.

MODE C: Hybrid (RECOMMENDED)

  - issue_assigned  --> spawn_opencode() (code changes needed)
  - pr_assigned     --> spawn_hermes()   (review, no code changes)
  - pr_comment      --> depends on context
  - rescue agent    --> spawn_hermes()   (diagnosis, not editing)

  Add WORKER_MODE to config.env: hermes|opencode|hybrid

### Phase 5: Post-Edit Orchestrator Step

After OpenCode finishes, the relay needs to:

  1. Check what files changed (git diff in the worktree)
  2. GPG-sign the commit (OpenCode might not handle this)
  3. Push to origin (fork)
  4. Create PR via gh CLI
  5. Comment on the issue

Recommended: relay does steps 2-5 directly in Python (~10 lines).
No need to spawn another agent for orchestration.

  # In relay.py after spawn_opencode() returns:
  worktree_dir, results, rc = spawn_opencode(prompt, provider, model, scope)

  if rc == 0:
      # Sign and push
      subprocess.run(["git", "add", "-A"], cwd=worktree_dir)
      subprocess.run(
          ["git", "commit", "-S", "-m", f"fix: {desc} (closes #{issue_num})"],
          cwd=worktree_dir
      )
      subprocess.run(
          ["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
          cwd=worktree_dir
      )
      # Create PR
      subprocess.run([
          "gh", "pr", "create",
          "--repo", "security-alliance/frameworks",
          "--head", f"frameworks-volunteer:{branch}",
          "--base", "develop",
          "--title", f"fix: {desc}",
          "--body", f"Closes #{issue_num}",
      ])
      # Comment on issue
      subprocess.run([
          "gh", "issue", "comment", str(issue_num),
          "--repo", "security-alliance/frameworks",
          "--body", f"PR opened targeting develop.",
      ])

### Phase 6: Session Resume for Iteration

If OpenCode's first attempt fails (conflict, CI failure), use
--continue to resume:

  opencode run --continue "The CI failed because..."
    --model openrouter/z-ai/glm-5.1
    --format json
    --cwd ~/frameworks/.worktrees/oc-XXXX

The relay could track the OpenCode session ID from the JSON output
and re-spawn with --continue for follow-up attempts.

## Permission Comparison

Current (Hermes + PTY auto-deny):
- ALL commands allowed by default
- Dangerous ones denied via PTY pattern matching
- Easy to miss edge cases
- Denied commands waste agent turns

Proposed (OpenCode permission config):
- Only ALLOWED commands run by default
- Denied at the tool level, not after the fact
- Granular per-command rules
- No wasted turns -- denied commands never start
- edit=allow with path restrictions possible

## Risks and Open Questions

1. ONE-SHOT LIMITATION
   opencode run is one prompt, one run. If it needs iteration
   (conflict resolution, CI failure fix), the relay must re-spawn
   with --continue. Hermes can iterate 90 turns in one session.

2. MODEL COMPATIBILITY
   Need to verify glm-5.1, MiniMax-M2.7, kimi-k2.5 work with
   OpenCode's tool calling format. OpenCode uses models.dev
   provider list.

3. ARM64 / LSP SUPPORT
   OpenCode npm package should work on arm64. But LSP servers it
   downloads (typescript-language-server, etc.) might not have
   arm64 binaries for all platforms.

4. NO SKILL SYSTEM (yet)
   OpenCode has its own "skills" concept but different from Hermes.
   Would need to convert frameworks-reactive-github skill into
   OpenCode rules (OPENCODERULES file or inline config).

5. COST
   OpenCode might use more tokens (LSP context, tool definitions).
   Need to benchmark against Hermes for the same task.

6. GPG SIGNING
   OpenCode's git operations might not respect commit.gpgsign=true.
   Need to verify, or have the relay amend the commit with -S after.

7. BASH=ASK IN UNATTENDED MODE
   For the relay, we probably want bash=allow (not ask) since
   there's no human. But OPENCODE_PERMISSION env var can override
   the file config to allow everything except catastrophic commands.

## Implementation Order

1. Install opencode, verify arm64 + OpenRouter models work
2. Create opencode.json with permission config, test locally
3. Add spawn_opencode() to relay.py (alongside spawn_hermes)
4. Add WORKER_MODE config: hermes|opencode|hybrid
5. Start with Mode C (hybrid): OpenCode for issue-assigned,
   Hermes for reviews/rescue
6. Add relay-level post-edit steps (sign, push, PR)
7. Benchmark: turns, time, cost, quality vs Hermes-only
8. If results good, expand to more event types
