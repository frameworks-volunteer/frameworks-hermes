#!/usr/bin/env python3
"""
Frameworks GitHub Webhook Relay

Receives GitHub webhook deliveries, validates signatures, enforces
whitelist, classifies events, chooses model, and spawns Hermes in
one-shot mode.
"""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import threading
import pty
import select
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config():
    env_file = Path(__file__).parent / "config.env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if val and key not in os.environ:
                    os.environ[key] = val

load_config()

WEBHOOK_SECRET  = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
ALLOWED_REPO    = os.environ.get("ALLOWED_REPO", "security-alliance/frameworks")
BOT_USERNAME    = os.environ.get("BOT_USERNAME", "frameworks-volunteer")
ALLOWED_SENDERS = [s.strip().lower() for s in os.environ.get("ALLOWED_SENDERS", "").split(",") if s.strip()]
# Model fallback chain: tried in order until one works.
# Format: provider/model, comma-separated. First is primary.
MODEL_CHAIN = [
    tuple(m.strip().split("/", 1))
    for m in os.environ.get(
        "MODEL_CHAIN",
        "openrouter/z-ai/glm-5.1,"
        "openrouter/minimax/MiniMax-M2.7,"
        "openrouter/kimi-coding-cn/kimi-k2.5"
    ).split(",") if m.strip()
]
# Backwards compat: single DEFAULT_MODEL still works
if os.environ.get("DEFAULT_MODEL"):
    dp = os.environ.get("DEFAULT_PROVIDER", "openrouter")
    MODEL_CHAIN.insert(0, (dp, os.environ["DEFAULT_MODEL"]))

# Self-review alternates (used when reviewing bot's own PRs)
SELF_REVIEW_MODELS = [
    tuple(m.strip().split("/", 1))
    for m in os.environ.get(
        "SELF_REVIEW_MODELS",
        "openrouter/minimax/MiniMax-M2.7,"
        "openrouter/kimi-coding-cn/kimi-k2.5"
    ).split(",") if m.strip()
]
HERMES_BIN      = os.environ.get("HERMES_BIN", "/home/zealot/.local/bin/hermes")
REPO_PATH       = os.environ.get("REPO_PATH", "/home/zealot/frameworks")
RELAY_PORT      = int(os.environ.get("RELAY_PORT", "9191"))
DELIVERY_DB     = os.environ.get("DELIVERY_DB", str(Path(__file__).parent / "deliveries.db"))
LOG_FILE        = os.environ.get("LOG_FILE", str(Path(__file__).parent / "relay.log"))
DANGEROUS_CMD_LOG = os.environ.get("DANGEROUS_CMD_LOG", str(Path(__file__).parent / "dangerous_cmds.log"))
STUCK_TIMEOUT  = int(os.environ.get("STUCK_TIMEOUT", "180"))  # seconds with no output before rescue
MAX_CONCURRENT  = int(os.environ.get("MAX_CONCURRENT", "3"))  # max parallel Hermes processes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("relay")

# ---------------------------------------------------------------------------
# Deduplication DB
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DELIVERY_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS deliveries "
        "(id TEXT PRIMARY KEY, ts REAL NOT NULL)"
    )
    conn.commit()
    return conn

def is_duplicate(delivery_id: str) -> bool:
    conn = sqlite3.connect(DELIVERY_DB)
    row = conn.execute(
        "SELECT id FROM deliveries WHERE id=?", (delivery_id,)
    ).fetchone()
    if row:
        return True
    conn.execute(
        "INSERT INTO deliveries (id, ts) VALUES (?, ?)",
        (delivery_id, time.time()),
    )
    conn.commit()
    conn.close()
    return False

def prune_db(max_age_hours: int = 48):
    cutoff = time.time() - (max_age_hours * 3600)
    conn = sqlite3.connect(DELIVERY_DB)
    conn.execute("DELETE FROM deliveries WHERE ts < ?", (cutoff,))
    conn.commit()
    conn.close()
    # Also prune old spawn logs
    spawn_dir = Path(__file__).parent / "spawns"
    if spawn_dir.exists():
        for f in spawn_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()

# ---------------------------------------------------------------------------
# Work queue + concurrency control
# ---------------------------------------------------------------------------

import queue

work_queue = queue.Queue()
concurrency_sem = threading.Semaphore(MAX_CONCURRENT)
active_spawns = {}  # spawn_id -> {thread, start_time, last_output_time, spawn_id}

def enqueue_work(classified, payload, provider, model, reasoning,
                 self_review, sender):
    """Add a work item to the queue. Returns immediately."""
    work_queue.put({
        "classified": classified,
        "payload": payload,
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "self_review": self_review,
        "sender": sender,
    })

def worker_loop():
    """Worker thread: pulls from queue, acquires semaphore, spawns Hermes."""
    while True:
        item = work_queue.get()
        if item is None:
            break
        concurrency_sem.acquire()
        try:
            _process_work_item(item)
        finally:
            concurrency_sem.release()
            work_queue.task_done()

def _process_work_item(item):
    """Process a single work item with model fallback."""
    classified = item["classified"]
    payload = item["payload"]
    provider = item["provider"]
    model = item["model"]
    reasoning = item["reasoning"]
    self_review = item["self_review"]
    sender = item["sender"]

    if self_review == "1" and SELF_REVIEW_MODELS:
        model_list = SELF_REVIEW_MODELS
    else:
        model_list = MODEL_CHAIN

    primary = (provider, model)
    try_order = [primary]
    for p, m in model_list:
        if (p, m) != primary:
            try_order.append((p, m))

    for i, (prov, mod) in enumerate(try_order):
        log.info("Processing: scope=%s provider=%s model=%s "
                 "sender=%s (attempt %d/%d)",
                 classified["scope"], prov, mod, sender,
                 i + 1, len(try_order))
        result = spawn_hermes(
            build_prompt(classified, prov, mod, reasoning,
                         self_review, payload),
            prov, mod, scope=classified["scope"],
        )
        if result is True:
            log.info("Completed: %s (model=%s/%s)",
                     classified["scope"], prov, mod)
            return
        elif result is None:
            log.warning("Rate limited on %s/%s, trying fallback",
                        prov, mod)
            continue
        else:
            log.error("Failed: %s (model=%s/%s)",
                      classified["scope"], prov, mod)
            return

    log.error("All models exhausted for: %s", classified["scope"])

# Start worker threads (one per MAX_CONCURRENT slot)
for _ in range(MAX_CONCURRENT):
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

# ---------------------------------------------------------------------------
# Rescue / watchdog for stuck spawns
# ---------------------------------------------------------------------------

def spawn_rescue(stuck_spawn_id: str, stuck_log_file: str,
                 stuck_prompt_file: str):
    """Spawn a lightweight rescue agent that reads the stuck spawn's
    context and either continues the work or diagnoses the hang.
    Uses the next model in the chain (not the one that got stuck)."""
    rescue_id = time.strftime("%Y%m%d_%H%M%S") + f"_rescue_{stuck_spawn_id}"
    spawn_dir = Path(__file__).parent / "spawns"
    spawn_dir.mkdir(exist_ok=True)
    rescue_log = spawn_dir / f"{rescue_id}_output.log"
    rescue_prompt_file = spawn_dir / f"{rescue_id}_prompt.txt"

    # Read stuck context
    stuck_output = ""
    stuck_prompt = ""
    try:
        stuck_output = Path(stuck_log_file).read_text()[:8000]
    except Exception:
        pass
    try:
        stuck_prompt = Path(stuck_prompt_file).read_text()[:2000]
    except Exception:
        pass

    # Check if original spawn already submitted a review or comment
    original_already_acted = False
    action_summary = ""
    if "gh pr review" in stuck_output and "successfully" in stuck_output.lower():
        original_already_acted = True
        action_summary = "The original session already submitted a PR review."
    elif "gh pr comment" in stuck_output and "successfully" in stuck_output.lower():
        original_already_acted = True
        action_summary = "The original session already submitted a PR comment."
    elif "gh issue comment" in stuck_output and "successfully" in stuck_output.lower():
        original_already_acted = True
        action_summary = "The original session already submitted an issue comment."

    rescue_prompt = (
        "You are a rescue agent. Another Hermes session got stuck or hung.\n"
        "\n"
        f"STUCK SPAWN: {stuck_spawn_id}\n"
        "\n"
    )
    if original_already_acted:
        rescue_prompt += (
            f"*** IMPORTANT: {action_summary} ***\n"
            "Do NOT submit another review or comment. The task is already done.\n"
            "Only clean up (switch branches, remove worktrees) if needed.\n"
            "If the original session's output looks complete, just exit.\n"
            "\n"
        )
    rescue_prompt += (
        "ORIGINAL PROMPT (first 2000 chars):\n"
        f"{stuck_prompt}\n"
        "\n"
        "OUTPUT SO FAR (last 8000 chars):\n"
        f"{stuck_output}\n"
        "\n"
        "Diagnose why it got stuck. Common causes:\n"
        "  - Waiting on a dangerous command prompt (should be auto-denied)\n"
        "  - API rate limit or timeout\n"
        "  - Infinite loop or retry loop\n"
        "  - Waiting for user input\n"
        "\n"
    )
    if not original_already_acted:
        rescue_prompt += (
            "If you need to continue the work (submit a review, comment, etc.):\n"
            "  - CHECK existing reviews/comments FIRST before submitting anything.\n"
            f"    Use: gh api repos/{ALLOWED_REPO}/pulls/NUMBER/reviews\n"
            "    Use: gh api repos/{ALLOWED_REPO}/issues/NUMBER/comments\n"
            "  - NEVER submit a duplicate review or comment.\n"
            f"  - If a review already exists from {BOT_USERNAME}, do NOT submit another.\n"
            "\n"
        )
    rescue_prompt += (
        "Then either:\n"
        "  1. Leave a comment on the issue/PR explaining what happened\n"
        "  2. Continue the work if you can (commit, push, PR)\n"
        "  3. If the original session already completed the task, just exit\n"
        "\n"
        f"Repo is at: {REPO_PATH}\n"
        "Use gh CLI for GitHub API calls.\n"
        "GH BODY RULE: Never use --body with inline text. Always use --body-file\n"
        "with a heredoc to prevent shell expansion of backticks:\n"
        "  cat > /tmp/${SPAWN_ID}_body.md << 'EOF'\n"
        "  (content)\n"
        "  EOF\n"
        "  gh pr review NUM --approve --body-file /tmp/${SPAWN_ID}_body.md\n"
        "Every response MUST start with: "
        "**Model:** `rescue` **Reasoning:** `high` **Provider:** `rescue`\n"
    )

    rescue_prompt_file.write_text(rescue_prompt)

    # Use a different model than the one that got stuck
    rescue_models = MODEL_CHAIN[1:] if len(MODEL_CHAIN) > 1 else MODEL_CHAIN
    prov, mod = rescue_models[0]

    log.info("[rescue %s] Spawning rescue agent: %s/%s", rescue_id, prov, mod)

    cmd = [
        HERMES_BIN, "chat",
        "--provider", prov,
        "--model", mod,
        "--skills", "frameworks-reactive-github,github-auth,github-issues,"
                    "github-pr-workflow,github-code-review",
        "--worktree",
        "--source", "tool",
        "--query", rescue_prompt,
        "--max-turns", "30",
    ]

    try:
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            text=True, cwd=REPO_PATH,
            env={**os.environ, "HERMES_MODEL": mod, "HERMES_PROVIDER": prov},
        )
        os.close(slave_fd)
        start = time.time()
        buf = ""
        with open(rescue_log, "w") as lf:
            while True:
                if (time.time() - start) > 300:  # 5 min max for rescue
                    proc.kill()
                    break
                if proc.poll() is not None:
                    try:
                        while True:
                            r, _, _ = select.select([master_fd], [], [], 0.5)
                            if not r:
                                break
                            chunk = os.read(master_fd, 4096).decode("utf-8", errors="replace")
                            buf += chunk
                    except (OSError, ValueError):
                        pass
                    break
                try:
                    r, _, _ = select.select([master_fd], [], [], 1.0)
                    if not r:
                        continue
                    chunk = os.read(master_fd, 4096).decode("utf-8", errors="replace")
                    buf += chunk
                    # Auto-deny dangerous commands in rescue too
                    if "DANGEROUS COMMAND:" in chunk and "Choice [o/s/D]:" in chunk:
                        os.write(master_fd, b"d\n")
                        log.warning("[rescue %s] DANGEROUS DENIED", rescue_id)
                except (OSError, ValueError):
                    break
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    lf.write(line + "\n")
                lf.flush()
        try:
            os.close(master_fd)
        except OSError:
            pass
        proc.wait(timeout=10)
        log.info("[rescue %s] Done: exit=%d", rescue_id, proc.returncode)
    except Exception as e:
        log.error("[rescue %s] Failed: %s", rescue_id, e)

# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def verify_signature(body: bytes, signature_header: str) -> bool:
    if not WEBHOOK_SECRET:
        log.warning("No WEBHOOK_SECRET configured -- skipping verification")
        return True
    if not signature_header:
        return False
    sha_name, sig = signature_header.split("=", 1) if "=" in signature_header else ("", "")
    if sha_name != "sha256":
        return False
    expected = hmac.new(
        WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)

# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------

def classify_event(event_type: str, action: str, payload: dict) -> dict | None:
    """
    Returns a dict with event classification or None if the event should be
    ignored. Does NOT check the whitelist -- that's done by the caller.
    """
    repo = payload.get("repository", {}).get("full_name", "")

    if event_type == "ping":
        return None

    if event_type == "issues":
        if action == "assigned":
            assignee = payload.get("assignee", {}) or {}
            if assignee.get("login", "").lower() == BOT_USERNAME.lower():
                return {
                    "scope": "issue_assigned",
                    "issue_number": payload.get("issue", {}).get("number"),
                    "issue_title": payload.get("issue", {}).get("title", ""),
                    "repo": repo,
                }
        return None

    if event_type == "pull_request":
        if action == "assigned":
            assignee = payload.get("assignee", {}) or {}
            if assignee.get("login", "").lower() == BOT_USERNAME.lower():
                pr = payload.get("pull_request", {})
                return {
                    "scope": "pr_assigned",
                    "pr_number": pr.get("number"),
                    "pr_title": pr.get("title", ""),
                    "pr_author": pr.get("user", {}).get("login", ""),
                    "repo": repo,
                }
        if action == "review_requested":
            reviewer = payload.get("requested_reviewer", {}) or {}
            if reviewer.get("login", "").lower() == BOT_USERNAME.lower():
                pr = payload.get("pull_request", {})
                return {
                    "scope": "pr_review_requested",
                    "pr_number": pr.get("number"),
                    "pr_title": pr.get("title", ""),
                    "pr_author": pr.get("user", {}).get("login", ""),
                    "repo": repo,
                }
        return None

    if event_type == "issue_comment":
        body = payload.get("comment", {}).get("body", "")
        mentions_bot = f"@{BOT_USERNAME}" in body
        explicit_request = any(
            kw in body.lower()
            for kw in ["please fix", "please review", "please look", "take a look",
                        "can you", "could you", "needs review",
                        BOT_USERNAME.lower()]
        )
        if not (mentions_bot or explicit_request):
            return None
        is_pr = "pull_request" in payload.get("issue", {})
        issue_number = payload.get("issue", {}).get("number")
        return {
            "scope": "pr_comment" if is_pr else "issue_comment",
            "issue_number": issue_number,
            "comment_body": body,
            "is_pr_comment": is_pr,
            "repo": repo,
        }

    if event_type == "pull_request_review":
        body = payload.get("review", {}).get("body", "")
        mentions_bot = f"@{BOT_USERNAME}" in body
        explicit_request = any(
            kw in body.lower()
            for kw in ["please fix", "please review", "please look", "take a look",
                        "can you", "could you", "needs review",
                        BOT_USERNAME.lower()]
        )
        if not (mentions_bot or explicit_request):
            return None
        pr = payload.get("pull_request", {})
        return {
            "scope": "pr_review",
            "pr_number": pr.get("number"),
            "pr_title": pr.get("title", ""),
            "review_body": body,
            "repo": repo,
        }

    if event_type == "pull_request_review_comment":
        body = payload.get("comment", {}).get("body", "")
        mentions_bot = f"@{BOT_USERNAME}" in body
        explicit_request = any(
            kw in body.lower()
            for kw in ["please fix", "please review", "please look", "take a look",
                        "can you", "could you", "needs review",
                        BOT_USERNAME.lower()]
        )
        if not (mentions_bot or explicit_request):
            return None
        pr = payload.get("pull_request", {})
        return {
            "scope": "pr_review_comment",
            "pr_number": pr.get("number"),
            "comment_body": body,
            "repo": repo,
        }

    return None

# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

def choose_model(classified: dict, payload: dict) -> tuple[str, str, str, str]:
    """
    Returns (provider, model, reasoning_level, self_review_flag).
    If self-review (PR authored by bot), picks from SELF_REVIEW_MODELS.
    Otherwise picks the primary model from MODEL_CHAIN (fallback happens
    in _run_with_fallback, not here).
    """
    is_self_review = (
        classified["scope"] == "pr_review_requested"
        and payload.get("pull_request", {}).get("user", {}).get("login", "").lower()
            == BOT_USERNAME.lower()
    )

    if is_self_review and SELF_REVIEW_MODELS:
        # Deterministic alternation across self-review models
        pr_num = payload.get("pull_request", {}).get("number", 0)
        provider, model = SELF_REVIEW_MODELS[pr_num % len(SELF_REVIEW_MODELS)]
        reasoning = "high"
        return provider, model, reasoning, "1"

    # Default: primary model from chain
    provider, model = MODEL_CHAIN[0]
    reasoning = "high"
    return provider, model, reasoning, "0"

# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_prompt(classified: dict, provider: str, model: str,
                 reasoning: str, self_review: str, payload: dict) -> str:
    """Build a one-shot prompt for Hermes."""
    scope = classified["scope"]
    lines = [
        "You are a reactive GitHub agent for "
        "security-alliance/frameworks.",
        "",
        "Load the skill: frameworks-reactive-github",
        "Also load: github-auth, github-issues, github-pr-workflow, "
        "github-code-review",
        "",
        f"Event scope: {scope}",
        f"**Model:** `{model}`  **Reasoning:** `{reasoning}`  **Provider:** `{provider}`",
        "",
        "FORK WORKFLOW:",
        f"  - origin = {BOT_USERNAME}/frameworks (fork, push here)",
        "  - upstream = security-alliance/frameworks (official, PRs/issues here)",
        "  - NEVER push to upstream. Always push branches to origin, then open PRs.",
        f"  - Use: gh pr create --repo security-alliance/frameworks --head {BOT_USERNAME}:BRANCH",
        "  - ALL COMMITS MUST BE GPG-SIGNED: always use git commit -S",
        "",
    ]

    if self_review == "1":
        lines.append(f"SELF-REVIEW: This PR was authored by {BOT_USERNAME}.")
        lines.append(f"You MUST use `{provider}/{model}` (not the default model).")
        lines.append("")

    # Scope-specific instructions
    if scope == "issue_assigned":
        num = classified["issue_number"]
        lines += [
            f"Issue #{num} was assigned to you: {classified.get('issue_title', '')}",
            "",
            "Follow Procedure 1 from the skill:",
            "1. Inspect the issue and repo context",
            "2. Create a branch from develop",
            "3. Implement the fix",
            "4. Quick checks only (lint/syntax, NOT full builds)",
            "5. GPG-SIGN your commit (git commit -S, ALWAYS)",
            "6. Push to fork (origin), create PR to upstream",
            "7. Leave a concise status comment",
            "",
            f"Repo is at: {REPO_PATH}",
            f"Use: gh issue view {num} --repo {ALLOWED_REPO}",
        ]
    elif scope in ("pr_assigned", "pr_review_requested"):
        num = classified["pr_number"]
        lines += [
            f"PR #{num} {'assigned to you' if scope == 'pr_assigned' else 'review requested from you'}: {classified.get('pr_title', '')}",
            f"PR author: {classified.get('pr_author', 'unknown')}",
            "",
            "Follow Procedure 2/3 from the skill:",
            "1. Fetch PR details",
            "2. Run security review (Procedure 4)",
            "3. Run QA review (Procedure 5)",
            "4. Before submitting, CHECK for existing reviews from this bot:",
            f"   gh api repos/{ALLOWED_REPO}/pulls/{num}/reviews --jq '.[] | select(.user.login==\"{BOT_USERNAME}\") | .id'",
            "   If a review already exists, do NOT submit another. Comment instead.",
            "5. Submit ONE review with the mandatory prefix",
            "",
            f"Repo is at: {REPO_PATH}",
            f"Use: gh pr view {num} --repo {ALLOWED_REPO}",
        ]
    elif scope == "issue_comment":
        num = classified["issue_number"]
        lines += [
            f"Comment on issue #{num} mentions @{BOT_USERNAME}",
            "",
            "Follow Procedure 6 from the skill:",
            "1. Read the issue and prior comments",
            "2. Answer or take action as appropriate",
            "3. Include the mandatory prefix",
            "",
            f"Comment body: {classified.get('comment_body', '')[:500]}",
            f"Repo is at: {REPO_PATH}",
            f"Use: gh issue view {num} --repo {ALLOWED_REPO}",
        ]
    elif scope == "pr_comment":
        num = classified["issue_number"]
        lines += [
            f"Comment on PR thread #{num} mentions @{BOT_USERNAME}",
            "",
            "Follow Procedure 6/8 from the skill:",
            "1. Read the PR and prior comments",
            "2. Re-review or respond as appropriate",
            "3. Include the mandatory prefix",
            "",
            f"Comment body: {classified.get('comment_body', '')[:500]}",
            f"Repo is at: {REPO_PATH}",
            f"Use: gh pr view {num} --repo {ALLOWED_REPO}",
        ]
    elif scope == "pr_review":
        num = classified["pr_number"]
        lines += [
            f"Review on PR #{num} mentions @{BOT_USERNAME}",
            "",
            "Follow Procedure 7 from the skill:",
            "1. Read the review context",
            "2. Reassess or chime in",
            "3. Include the mandatory prefix",
            "",
            f"Review body: {classified.get('review_body', '')[:500]}",
            f"Repo is at: {REPO_PATH}",
            f"Use: gh pr view {num} --repo {ALLOWED_REPO}",
        ]
    elif scope == "pr_review_comment":
        num = classified["pr_number"]
        lines += [
            f"Review comment on PR #{num} mentions @{BOT_USERNAME}",
            "",
            "Follow Procedure 7 from the skill:",
            "1. Read the comment context",
            "2. Reassess or chime in",
            "3. Include the mandatory prefix",
            "",
            f"Comment body: {classified.get('comment_body', '')[:500]}",
            f"Repo is at: {REPO_PATH}",
            f"Use: gh pr view {num} --repo {ALLOWED_REPO}",
        ]

    lines.append("")
    lines.append("Every GitHub response MUST start with this line (bold + code):")
    lines.append(f"  **Model:** `{model}` **Reasoning:** `{reasoning}` **Provider:** `{provider}`")
    lines.append("")
    lines.append("Work in the repo directory. Use gh CLI for all GitHub API calls.")
    lines.append("")
    lines.append("GH BODY RULE: Never use --body with inline text (double quotes mangle")
    lines.append("backticks as command substitution). Always use --body-file with a heredoc:")
    lines.append("  cat > /tmp/${SPAWN_ID}_body.md << 'EOF'")
    lines.append("  (content here)")
    lines.append("  EOF")
    lines.append("  gh pr review NUM --approve --body-file /tmp/${SPAWN_ID}_body.md")
    lines.append("The single-quoted 'EOF' prevents ALL shell expansion. Use $SPAWN_ID in")
    lines.append("filenames to avoid collisions if multiple spawns run concurrently.")
    lines.append("")
    lines.append("When done, exit. Do not wait for further input.")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Hermes spawner
# ---------------------------------------------------------------------------

def spawn_hermes(prompt: str, provider: str, model: str,
                 scope: str = "") -> bool:
    """Spawn Hermes in one-shot mode and wait for it to finish.

    Writes the prompt and full Hermes output to per-spawn files under
    spawns/ so you can inspect what happened. Logs key events (tool
    calls, session ID, errors) to the relay log in real time.
    """
    spawn_id = time.strftime("%Y%m%d_%H%M%S")
    if scope:
        spawn_id += f"_{scope}"

    spawn_dir = Path(__file__).parent / "spawns"
    spawn_dir.mkdir(exist_ok=True)
    prompt_file = spawn_dir / f"{spawn_id}_prompt.txt"
    log_file    = spawn_dir / f"{spawn_id}_output.log"

    # Save prompt
    prompt_file.write_text(prompt)
    log.info("Prompt saved: %s", prompt_file.name)

    cmd = [
        HERMES_BIN,
        "chat",
        "--provider", provider,
        "--model", model,
        "--skills", "frameworks-reactive-github,github-auth,github-issues,"
                    "github-pr-workflow,github-code-review",
        "--worktree",
        "--checkpoints",
        "--source", "tool",
        "--query", prompt,
        "--max-turns", "90",
    ]

    log.info("Spawning Hermes: %s/%s scope=%s spawn=%s",
             provider, model, scope, spawn_id)

    try:
        # Run with a PTY so we can detect and auto-deny dangerous command
        # prompts. When Hermes flags a command as dangerous, it shows an
        # interactive [o]nce|[s]ession|[d]eny prompt. We auto-deny by
        # sending 'd\n' to the PTY, log the blocked command, and let
        # Hermes continue (it will see the denial and find an alternative).
        MAX_SPAWN_SECONDS = 900  # Hard kill after 15 minutes

        # Pattern to detect dangerous command prompts
        DANGEROUS_PATTERN = re.compile(
            r"DANGEROUS COMMAND:.*?Choice \[o/s/D\]:",
            re.DOTALL,
        )
        # Simpler line-by-line triggers
        DANGEROUS_LINE = "DANGEROUS COMMAND:"
        CHOICE_LINE = "Choice [o/s/D]:"

        # Create a PTY
        master_fd, slave_fd = pty.openpty()

        with open(log_file, "w") as lf:
            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                text=True,
                cwd=REPO_PATH,
                env={**os.environ,
                     "HERMES_MODEL": model,
                     "HERMES_PROVIDER": provider,
                     # Prevent git from trying to open an interactive editor
                     # during rebase --continue or commit --amend. Without
                     # these, git falls back to nano which fails on a PTY
                     # ("dumb terminal"), causing rebase --continue to error.
                     "GIT_EDITOR": "true",
                     "GIT_SEQUENCE_EDITOR": "true",
                     "EDITOR": "true",
                     "VISUAL": "true",
                     "SPAWN_ID": spawn_id},
            )
            # Close slave in parent -- child has its copy
            os.close(slave_fd)

            session_id = None
            tool_count = 0
            dangerous_denied_count = 0
            start_time = time.time()
            last_output_time = time.time()
            stuck_rescue_sent = False
            buf = ""  # accumulate partial lines
            pending_danger = None  # track multi-line dangerous prompt
            completed_actions = set()  # track submitted reviews/comments

            def _timed_out():
                return (time.time() - start_time) > MAX_SPAWN_SECONDS

            def _log_dangerous(cmd_text: str):
                """Log a denied dangerous command to relay log and
                the dedicated dangerous_cmds audit file."""
                nonlocal dangerous_denied_count
                dangerous_denied_count += 1
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                cmd_text = cmd_text.strip()[:300]
                log.warning("[spawn %s] DANGEROUS DENIED (#%d): %s",
                            spawn_id, dangerous_denied_count,
                            cmd_text[:150])
                # Append to dedicated audit log
                with open(DANGEROUS_CMD_LOG, "a") as dlf:
                    dlf.write(f"[{ts}] [{spawn_id}] DENIED: {cmd_text}\n")

            while True:
                # Check if stuck (no output for STUCK_TIMEOUT seconds)
                stuck_duration = time.time() - last_output_time
                if (stuck_duration > STUCK_TIMEOUT and
                        not stuck_rescue_sent):
                    log.warning("[spawn %s] STUCK: no output for %ds -- "
                                "spawning rescue agent, killing original",
                                spawn_id, int(stuck_duration))
                    stuck_rescue_sent = True
                    # Kill the stuck spawn immediately to prevent it from
                    # racing with the rescue agent (e.g., both submitting
                    # reviews on the same PR).
                    try:
                        proc.kill()
                        log.info("[spawn %s] Killed stuck process (PID %d)",
                                 spawn_id, proc.pid)
                    except OSError:
                        pass
                    # Spawn rescue in a separate thread
                    rt = threading.Thread(
                        target=spawn_rescue,
                        args=(spawn_id, str(log_file), str(prompt_file)),
                        daemon=True,
                    )
                    rt.start()

                if _timed_out():
                    log.error("[spawn %s] Hard timeout (%ds) -- killing",
                              spawn_id, MAX_SPAWN_SECONDS)
                    proc.kill()
                    break

                # Check if process has exited AND PTY is drained
                if proc.poll() is not None:
                    # Drain remaining PTY output
                    while True:
                        try:
                            r, _, _ = select.select([master_fd], [], [], 0.5)
                            if not r:
                                break
                            chunk = os.read(master_fd, 4096).decode(
                                "utf-8", errors="replace")
                            buf += chunk
                        except (OSError, ValueError):
                            break
                    break

                # Read from PTY with a short timeout
                try:
                    r, _, _ = select.select([master_fd], [], [], 1.0)
                    if not r:
                        continue
                    chunk = os.read(master_fd, 4096).decode(
                        "utf-8", errors="replace")
                    buf += chunk
                    last_output_time = time.time()
                except (OSError, ValueError):
                    # PTY closed
                    break

                # Process complete lines from buffer
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line += "\n"
                    lf.write(line)
                    lf.flush()
                    stripped = line.rstrip()

                    # Detect dangerous command prompt
                    if DANGEROUS_LINE in stripped:
                        pending_danger = stripped
                        continue
                    if pending_danger is not None:
                        pending_danger += "\n" + stripped
                        if CHOICE_LINE in stripped:
                            # Full prompt captured -- auto-deny
                            _log_dangerous(pending_danger)
                            os.write(master_fd, b"d\n")
                            pending_danger = None
                            continue
                        # Still accumulating the multi-line prompt
                        continue

                    # Extract session ID
                    if stripped.startswith("Session:") and session_id is None:
                        session_id = stripped.split()[-1]
                        log.info("[spawn %s] Session: %s", spawn_id,
                                 session_id)

                    # Log tool calls (terminal, file ops, API calls)
                    if "$" in stripped and ("terminal" in stripped.lower()
                            or "git" in stripped.lower()
                            or "gh " in stripped.lower()):
                        tool_count += 1
                        log.info("[spawn %s] Tool: %s", spawn_id,
                                 stripped.strip()[:120])

                        # Detect duplicate review/comment submissions.
                        # If the agent already submitted one and tries
                        # again, kill the spawn to prevent spam.
                        for action_type, pattern in [
                            ("review", "gh pr review"),
                            ("pr_comment", "gh pr comment"),
                            ("issue_comment", "gh issue comment"),
                        ]:
                            if pattern in stripped:
                                if action_type in completed_actions:
                                    log.error(
                                        "[spawn %s] DUPLICATE %s detected "
                                        "-- killing spawn to prevent spam",
                                        spawn_id, action_type)
                                    try:
                                        proc.kill()
                                    except OSError:
                                        pass
                                else:
                                    completed_actions.add(action_type)

                    # Log Hermes responses (assistant output)
                    elif "Hermes" in stripped and "─" not in stripped and stripped.strip():
                        log.info("[spawn %s] Reply: %s", spawn_id,
                                 stripped.strip()[:150])

                    # Log errors immediately
                    elif any(kw in stripped for kw in
                             ["Error", "error:", "403", "401", "404",
                              "failed", "Traceback"]):
                        log.warning("[spawn %s] %s", spawn_id,
                                    stripped.strip()[:200])

                    # Log worktree creation/cleanup
                    elif "Worktree" in stripped:
                        log.info("[spawn %s] %s", spawn_id,
                                 stripped.strip()[:120])

            # Process any remaining buffer
            if buf.strip():
                lf.write(buf)
                lf.flush()

            # Close master FD
            try:
                os.close(master_fd)
            except OSError:
                pass

            # Wait for process to finish
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

            if dangerous_denied_count > 0:
                log.info("[spawn %s] Dangerous commands denied: %d",
                          spawn_id, dangerous_denied_count)

        # Parse session ID from output if we didn't catch it live
        if session_id is None:
            output_text = log_file.read_text()
            for ln in output_text.splitlines():
                if ln.strip().startswith("Session:"):
                    session_id = ln.strip().split()[-1]
                    break

        # Read summary stats from output
        output_text = log_file.read_text()
        duration = "?"
        msg_count = "?"
        for ln in output_text.splitlines():
            if ln.strip().startswith("Duration:"):
                duration = ln.strip().split()[-1]
            if ln.strip().startswith("Messages:"):
                msg_count = ln.strip().split()[-1]

        # Check for crashes -- distinguish rate-limit (retryable)
        # from fatal errors (auth, module, etc.)
        rate_limit_indicators = [
            "429", "rate limit", "Rate limit", "rate_limit",
            "Too Many Requests", "too many requests",
            "quota", "Quota exceeded", "capacity",
            "temporarily unavailable", "overloaded",
            "Error code: 429",
        ]
        fatal_indicators = [
            "API key was rejected", "token expired or incorrect",
            "Traceback (most recent call last)",
            "ModuleNotFoundError", "ImportError",
            "Invalid API key", "authentication failed",
        ]

        is_rate_limited = any(ind in output_text for ind in rate_limit_indicators)
        is_fatal = any(ind in output_text for ind in fatal_indicators)

        if is_fatal:
            log.error("[spawn %s] FATAL (exit %d) session=%s",
                      spawn_id, proc.returncode, session_id)
            return False

        if is_rate_limited:
            log.warning("[spawn %s] RATE LIMITED (exit %d) session=%s -- "
                        "will try fallback",
                        spawn_id, proc.returncode, session_id)
            return None  # None = retry with next model

        log.info("[spawn %s] Done: exit=%d session=%s "
                 "duration=%s msgs=%s tools=%d output=%s",
                 spawn_id, proc.returncode, session_id,
                 duration, msg_count, tool_count, log_file.name)
        return True

    except subprocess.TimeoutExpired:
        proc.kill()
        log.error("[spawn %s] Timed out after 600s", spawn_id)
        return False
    except Exception as e:
        log.error("[spawn %s] Spawn failed: %s", spawn_id, e)
        return False

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # 1. Verify signature
        sig = self.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(body, sig):
            log.warning("Invalid signature -- rejecting")
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"invalid signature")
            return

        # 2. Parse
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            log.warning("Invalid JSON -- rejecting")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"invalid json")
            return

        # 3. Dedupe
        delivery_id = self.headers.get("X-GitHub-Delivery", "")
        if delivery_id and is_duplicate(delivery_id):
            log.info("Duplicate delivery %s -- skipping", delivery_id)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"duplicate")
            return

        # 4. Check repository
        repo = payload.get("repository", {}).get("full_name", "")
        if repo.lower() != ALLOWED_REPO.lower():
            log.info("Wrong repo %s -- ignoring", repo)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"wrong repo")
            return

        # 5. Check event type
        event_type = self.headers.get("X-GitHub-Event", "")
        if event_type == "ping":
            log.info("Ping event -- acknowledging")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
            return

        # 6. Ignore bot's own events
        sender = ""
        if event_type == "issues":
            sender = payload.get("sender", {}).get("login", "")
        elif event_type == "pull_request":
            sender = payload.get("sender", {}).get("login", "")
        elif event_type in ("issue_comment", "pull_request_review",
                            "pull_request_review_comment"):
            sender = payload.get("sender", {}).get("login", "")
        elif event_type == "push":
            sender = payload.get("sender", {}).get("login", "")

        if sender.lower() == BOT_USERNAME.lower():
            log.info("Event from bot (%s) -- ignoring", sender)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"self-event")
            return

        # 7. Enforce whitelist
        if sender and sender.lower() not in ALLOWED_SENDERS:
            log.info("Sender %s not in whitelist -- ignoring", sender)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"not whitelisted")
            return

        # 8. Classify
        action = payload.get("action", "")
        classified = classify_event(event_type, action, payload)
        if classified is None:
            log.info("Event %s/%s not in scope -- ignoring", event_type, action)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"not in scope")
            return

        # 9. Choose model
        provider, model, reasoning, self_review = choose_model(classified, payload)

        # 10. Build prompt
        prompt = build_prompt(classified, provider, model, reasoning,
                              self_review, payload)

        # 11. Accept the webhook (respond before spawning)
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"accepted")

        # 12. Enqueue work item. Workers will pick it up, enforce
        # concurrency limits, and handle model fallback.
        queue_depth = work_queue.qsize()
        if queue_depth >= 10:
            log.warning("Queue depth %d -- high load", queue_depth)
        enqueue_work(classified, payload, provider, model,
                    reasoning, self_review, sender)
        log.info("Enqueued: scope=%s (queue depth: %d)",
                 classified["scope"], queue_depth + 1)

    def log_message(self, fmt, *args):
        # Suppress default access log, we use our own
        pass

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    init_db()
    # Prune old delivery IDs on startup
    prune_db()
    server = HTTPServer(("127.0.0.1", RELAY_PORT), WebhookHandler)
    log.info("Relay listening on 127.0.0.1:%d", RELAY_PORT)
    log.info("Repo: %s  Bot: %s  Whitelist: %s",
             ALLOWED_REPO, BOT_USERNAME, ALLOWED_SENDERS)
    log.info("Model chain: %s",
             " -> ".join(f"{p}/{m}" for p, m in MODEL_CHAIN))
    if SELF_REVIEW_MODELS:
        log.info("Self-review models: %s",
                 " -> ".join(f"{p}/{m}" for p, m in SELF_REVIEW_MODELS))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.server_close()

if __name__ == "__main__":
    main()
