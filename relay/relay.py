#!/usr/bin/env python3
"""
Frameworks GitHub Webhook Relay

Receives GitHub webhook deliveries, validates signatures, enforces
whitelist, classifies events, chooses model, and spawns Hermes in
one-shot mode with hermes --worktree isolation.
Up to MAX_CONCURRENT (default 3) agents run in parallel. After bot-fork
PRs merge, worktrees and head branches are deleted automatically.
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

# Model fallback chain: must be configured via MODEL_CHAIN env var.
# Format: provider/model[@reasoning], comma-separated. First is primary.
# Optional @level sets per-model reasoning effort (default: medium).
# Example: openrouter/x-ai/grok-4.5@high,openrouter/z-ai/glm-5.2@medium
_model_chain_raw = os.environ.get("MODEL_CHAIN", "")
if not _model_chain_raw:
    print("ERROR: MODEL_CHAIN not configured. Set it in config.env.", file=sys.stderr)
    sys.exit(1)


def _parse_chain_entry(entry: str) -> tuple[str, str, str]:
    """Parse 'provider/model[@reasoning]' -> (provider, model, reasoning)."""
    entry = entry.strip()
    reasoning = "medium"
    if "@" in entry:
        entry, _, level = entry.partition("@")
        if level.strip():
            reasoning = level.strip()
    provider, model = entry.split("/", 1)
    return (provider.strip(), model.strip(), reasoning)


MODEL_CHAIN = [
    _parse_chain_entry(m.strip())
    for m in _model_chain_raw.split(",") if m.strip()
]
if not MODEL_CHAIN:
    print("ERROR: MODEL_CHAIN is empty. Check config.env.", file=sys.stderr)
    sys.exit(1)

# Backwards compat: single DEFAULT_MODEL still works
if os.environ.get("DEFAULT_MODEL"):
    dp = os.environ.get("DEFAULT_PROVIDER", "openrouter")
    MODEL_CHAIN.insert(0, (dp, os.environ["DEFAULT_MODEL"], "medium"))

# Self-review alternates (used when reviewing bot's own PRs)
_self_review_raw = os.environ.get("SELF_REVIEW_MODELS", "")
if _self_review_raw:
    SELF_REVIEW_MODELS = [
        _parse_chain_entry(m.strip())
        for m in _self_review_raw.split(",") if m.strip()
    ]
else:
    # Default to MODEL_CHAIN[1:] if not configured
    SELF_REVIEW_MODELS = MODEL_CHAIN[1:] if len(MODEL_CHAIN) > 1 else MODEL_CHAIN[:]
HERMES_BIN      = os.environ.get("HERMES_BIN", "hermes")
REPO_PATH       = os.environ.get("REPO_PATH", "/home/zealot/frameworks")
RELAY_PORT      = int(os.environ.get("RELAY_PORT", "9191"))
DELIVERY_DB     = os.environ.get("DELIVERY_DB", str(Path(__file__).parent / "deliveries.db"))
LOG_FILE        = os.environ.get("LOG_FILE", str(Path(__file__).parent / "relay.log"))
DANGEROUS_CMD_LOG = os.environ.get("DANGEROUS_CMD_LOG", str(Path(__file__).parent / "dangerous_cmds.log"))
STUCK_TIMEOUT  = int(os.environ.get("STUCK_TIMEOUT", "180"))  # seconds with no output before rescue
MAX_CONCURRENT  = int(os.environ.get("MAX_CONCURRENT", "3"))  # max parallel Hermes processes
# Worktree / branch cleanup after bot PRs merge
WORKTREE_DIR = os.environ.get(
    "WORKTREE_DIR", str(Path(REPO_PATH) / ".worktrees")
)
CLEANUP_ON_MERGE = os.environ.get("CLEANUP_ON_MERGE", "1") not in ("0", "false", "False")
STALE_WORKTREE_HOURS = int(os.environ.get("STALE_WORKTREE_HOURS", "24"))
# How often the background reaper wakes (seconds)
WORKTREE_REAP_INTERVAL = int(os.environ.get("WORKTREE_REAP_INTERVAL", "3600"))

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

# Cancellation tracking for unassigned / review-request-removed events
cancelled_lock = threading.Lock()
cancelled_work = set()  # keys: ("issue", num) or ("pr", num)

def _work_item_key(item):
    """Return a cancellation key for a work item, or None."""
    classified = item.get("classified", {})
    scope = classified.get("scope", "")
    if scope == "issue_assigned":
        return ("issue", classified.get("issue_number"))
    elif scope in ("pr_assigned", "pr_review_requested"):
        return ("pr", classified.get("pr_number"))
    return None

def cancel_pending_work(classified):
    """Mark work items for a given issue/PR as cancelled and attempt
    to remove them from the in-memory queue. Returns number removed."""
    scope = classified.get("scope", "")
    if scope == "issue_unassigned":
        key = ("issue", classified.get("issue_number"))
    elif scope in ("pr_unassigned", "pr_review_request_removed"):
        key = ("pr", classified.get("pr_number"))
    else:
        return 0

    with cancelled_lock:
        cancelled_work.add(key)

    removed = 0
    temp = []
    while True:
        try:
            item = work_queue.get_nowait()
        except queue.Empty:
            break
        if _work_item_key(item) == key:
            removed += 1
        else:
            temp.append(item)

    for item in temp:
        work_queue.put(item)

    log.info("Cancelled %s: removed %d queued item(s)", key, removed)
    return removed


# ---------------------------------------------------------------------------
# Worktree + branch cleanup (post-merge + stale reaper)
# ---------------------------------------------------------------------------

def _run_git(*args, cwd=None, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd or REPO_PATH,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def is_bot_fork_pr(pr: dict) -> bool:
    """True if PR head lives on the bot fork (safe to delete head branch)."""
    head = pr.get("head") or {}
    repo = head.get("repo") or {}
    full = (repo.get("full_name") or "").lower()
    login = ((head.get("user") or {}).get("login") or "").lower()
    bot = BOT_USERNAME.lower()
    return full == f"{bot}/frameworks" or login == bot


def _list_worktrees() -> list[dict]:
    """Parse `git worktree list --porcelain` into dicts."""
    r = _run_git("worktree", "list", "--porcelain")
    if r.returncode != 0:
        log.warning("worktree list failed: %s", (r.stderr or "").strip()[:200])
        return []
    items = []
    cur = {}
    for line in (r.stdout or "").splitlines():
        if not line.strip():
            if cur.get("path"):
                items.append(cur)
            cur = {}
            continue
        if line.startswith("worktree "):
            cur["path"] = line[len("worktree "):]
        elif line.startswith("branch "):
            # refs/heads/foo
            ref = line[len("branch "):]
            cur["branch"] = ref.split("/", 2)[-1] if ref.startswith("refs/heads/") else ref
            cur["branch_ref"] = ref
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line == "bare":
            cur["bare"] = True
        elif line == "detached":
            cur["detached"] = True
        elif line == "locked" or line.startswith("locked "):
            cur["locked"] = True
            reason = line[len("locked"):].strip()
            if reason:
                cur["lock_reason"] = reason
    if cur.get("path"):
        items.append(cur)
    return items



def _worktree_lock_is_live(wt: dict) -> bool:
    """True if hermes pid= in lock reason is still running."""
    if not wt.get("locked"):
        return False
    reason = wt.get("lock_reason") or ""
    m = re.search(r"hermes pid=(\d+)", reason)
    if not m:
        return False  # unknown lock — allow reaper after age/unpushed gates
    pid = int(m.group(1))
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours — treat live


def _remove_worktree(path: str, reason: str = "") -> bool:
    """Unlock + force-remove a worktree path. Returns True on success."""
    if not path or path.rstrip("/") == Path(REPO_PATH).as_posix().rstrip("/"):
        return False
    p = Path(path)
    if not p.exists():
        # Still prune the registration
        _run_git("worktree", "prune")
        return True
    _run_git("worktree", "unlock", path)
    r = _run_git("worktree", "remove", "--force", path)
    if r.returncode != 0:
        # Fallback: manual rm + prune (locked/corrupt trees)
        log.warning("worktree remove failed (%s): %s — trying rm+prune",
                    reason, (r.stderr or "").strip()[:200])
        try:
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            log.error("rmtree %s failed: %s", path, e)
            return False
        _run_git("worktree", "prune")
    log.info("Removed worktree %s (%s)", path, reason or "cleanup")
    return True


def _delete_local_branch(branch: str) -> bool:
    if not branch or branch in ("develop", "main", "master"):
        return False
    # Never delete the branch currently checked out in primary tree
    cur = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    if (cur.stdout or "").strip() == branch:
        log.info("Skip delete local branch %s (checked out in primary)", branch)
        return False
    r = _run_git("branch", "-D", branch)
    if r.returncode == 0:
        log.info("Deleted local branch %s", branch)
        return True
    log.debug("local branch -D %s: %s", branch, (r.stderr or "").strip()[:160])
    return False


def _delete_remote_branch(branch: str, remote: str = "origin") -> bool:
    if not branch or branch in ("develop", "main", "master"):
        return False
    r = _run_git("push", remote, "--delete", branch, timeout=120)
    if r.returncode == 0:
        log.info("Deleted remote %s/%s", remote, branch)
        return True
    # already gone is fine
    err = (r.stderr or "") + (r.stdout or "")
    if "remote ref does not exist" in err or "does not exist" in err:
        log.info("Remote %s/%s already gone", remote, branch)
        return True
    log.warning("push --delete %s/%s failed: %s", remote, branch, err.strip()[:200])
    return False


def cleanup_worktrees_for_branch(branch: str, reason: str = "") -> int:
    """Remove any linked worktree that has `branch` checked out."""
    if not branch:
        return 0
    removed = 0
    for wt in _list_worktrees():
        if wt.get("branch") == branch and not wt.get("bare"):
            # skip primary
            if Path(wt["path"]).resolve() == Path(REPO_PATH).resolve():
                continue
            if _remove_worktree(wt["path"], reason=f"branch={branch} {reason}"):
                removed += 1
    return removed


def cleanup_after_merged_pr(pr: dict) -> dict:
    """After a bot-fork PR merges: drop worktrees + local/remote head branch.

    Only runs when pr.merged is true (closed-without-merge leaves branches).
    """
    stats = {"worktrees": 0, "local_branch": False, "remote_branch": False,
             "skipped": False, "branch": ""}
    if not CLEANUP_ON_MERGE:
        stats["skipped"] = True
        return stats
    if not pr.get("merged"):
        log.info("PR #%s closed without merge — leaving branches",
                 pr.get("number"))
        stats["skipped"] = True
        return stats
    if not is_bot_fork_pr(pr):
        log.info("PR #%s head not on bot fork — no cleanup", pr.get("number"))
        stats["skipped"] = True
        return stats

    head = pr.get("head") or {}
    branch = head.get("ref") or ""
    stats["branch"] = branch
    num = pr.get("number")
    log.info("Post-merge cleanup for PR #%s branch=%s", num, branch)

    # 1. Remove worktrees sitting on this branch
    stats["worktrees"] = cleanup_worktrees_for_branch(
        branch, reason=f"merged PR #{num}"
    )

    # 2. Also remove hermes/* session worktrees that still point at this tip
    #    (agent often commits on fix/* inside a hermes-* worktree path)
    for wt in _list_worktrees():
        b = wt.get("branch") or ""
        path = wt.get("path") or ""
        if Path(path).resolve() == Path(REPO_PATH).resolve():
            continue
        if b == branch or (b.startswith("hermes/") and branch and
                           branch in path):
            # extra: if worktree path is under .worktrees and branch is gone
            # remote-wise, leave to stale reaper unless branch matches
            pass

    # 3. Delete local + remote feature branch
    stats["local_branch"] = _delete_local_branch(branch)
    stats["remote_branch"] = _delete_remote_branch(branch, "origin")

    # 4. Prune worktree metadata
    _run_git("worktree", "prune")
    return stats


def prune_stale_hermes_worktrees(max_age_hours: int | None = None) -> dict:
    """Reap old hermes-* session worktrees left behind (crash / unpushed keep).

    Conservative: only removes worktrees under WORKTREE_DIR named hermes-*
    whose mtime is older than max_age_hours AND have no unique unpushed
    commits (best-effort). Always unlocks dead locks first.
    """
    import time as _time
    max_age = max_age_hours if max_age_hours is not None else STALE_WORKTREE_HOURS
    stats = {"removed": 0, "kept": 0, "errors": 0}
    root = Path(WORKTREE_DIR)
    if not root.is_dir():
        return stats
    now = _time.time()
    cutoff = now - (max_age * 3600)

    for wt in _list_worktrees():
        path = wt.get("path") or ""
        p = Path(path)
        if not path or p.resolve() == Path(REPO_PATH).resolve():
            continue
        try:
            p.relative_to(root.resolve())
        except ValueError:
            continue  # not under our worktree dir
        name = p.name
        if not (name.startswith("hermes-") or (wt.get("branch") or "").startswith("hermes/")):
            # feature worktrees (fix/*) are cleaned on merge, not by age
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0
        if mtime > cutoff:
            stats["kept"] += 1
            continue

        # Live hermes session lock — never touch
        if _worktree_lock_is_live(wt):
            log.debug("Keeping live-locked worktree %s (%s)", path, wt.get("lock_reason"))
            stats["kept"] += 1
            continue

        # Unpushed guard: keep if commits not on any remote
        chk = _run_git(
            "rev-list", "--max-count=1", "--not", "--remotes", "HEAD",
            cwd=path,
        )
        has_unpushed = bool((chk.stdout or "").strip()) and chk.returncode == 0
        if has_unpushed:
            # Second chance: if every commit is patch-equivalent upstream
            # (squash-merge case), still safe to drop — use git cherry
            cherry = _run_git("cherry", "-v", "upstream/develop", "HEAD", cwd=path)
            # lines starting with "+" are not upstream
            only_plus = [
                ln for ln in (cherry.stdout or "").splitlines()
                if ln.startswith("+")
            ]
            if only_plus:
                log.info("Keeping stale worktree %s (unpushed commits)", path)
                stats["kept"] += 1
                continue

        branch = wt.get("branch") or ""
        if _remove_worktree(path, reason=f"stale>{max_age}h"):
            stats["removed"] += 1
            if branch.startswith("hermes/"):
                _delete_local_branch(branch)
        else:
            stats["errors"] += 1

    _run_git("worktree", "prune")
    # Orphan hermes/* branches with no worktree
    br = _run_git("for-each-ref", "--format=%(refname:short)", "refs/heads/hermes")
    live_branches = {wt.get("branch") for wt in _list_worktrees()}
    for b in (br.stdout or "").splitlines():
        b = b.strip()
        if b and b not in live_branches:
            _delete_local_branch(b)
    return stats


def worktree_reaper_loop():
    """Background: periodically prune stale hermes session worktrees."""
    while True:
        try:
            time.sleep(WORKTREE_REAP_INTERVAL)
            stats = prune_stale_hermes_worktrees()
            if stats.get("removed") or stats.get("errors"):
                log.info("Worktree reaper: %s", stats)
        except Exception as e:
            log.error("Worktree reaper error: %s", e)


def enqueue_work(classified, payload, provider, model, reasoning,
                 self_review, sender):
    """Add a work item to the queue. Returns immediately."""
    key = None
    scope = classified.get("scope", "")
    if scope == "issue_assigned":
        key = ("issue", classified.get("issue_number"))
    elif scope in ("pr_assigned", "pr_review_requested"):
        key = ("pr", classified.get("pr_number"))

    if key:
        with cancelled_lock:
            cancelled_work.discard(key)

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
        key = _work_item_key(item)
        if key:
            with cancelled_lock:
                if key in cancelled_work:
                    log.info("Skipping cancelled work item: %s", key)
                    work_queue.task_done()
                    continue
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

    # try_order entries are (provider, model, reasoning) triples.
    primary = (provider, model)
    try_order = []
    for p, m, r_level in model_list:
        if (p, m) == primary:
            try_order.insert(0, (p, m, r_level))
        else:
            try_order.append((p, m, r_level))

    for i, (prov, mod, prov_reasoning) in enumerate(try_order):
        prompt = build_prompt(classified, prov, mod, prov_reasoning,
                              self_review, payload)
        # Cross-spawn dedup: if an earlier attempt in THIS chain already
        # posted the required review/comment for this exact target, do
        # not spawn again -- a re-post is duplicate spam (PR #620).
        if i > 0 and _action_already_posted(classified["scope"], prompt):
            log.warning("Skipping %s/%s for %s: required action already "
                        "posted by an earlier attempt (cross-spawn dedup)",
                        prov, mod, classified["scope"])
            continue
        log.info("Processing: scope=%s provider=%s model=%s "
                 "sender=%s (attempt %d/%d)",
                 classified["scope"], prov, mod, sender,
                 i + 1, len(try_order))
        result = spawn_hermes(
            prompt,
            prov, mod, scope=classified["scope"],
            reasoning=prov_reasoning,
        )
        if result is True:
            log.info("Completed: %s (model=%s/%s)",
                     classified["scope"], prov, mod)
            return
        elif result is None:
            log.warning("Retryable failure on %s/%s "
                        "(rate-limit or incomplete workflow), trying fallback",
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
        "GH BODY RULE: Never use --body with inline text. Use write_file to\n"
        "create /tmp/${SPAWN_ID}_body.md with the review text, then:\n"
        "  gh pr review NUM --approve --body-file /tmp/${SPAWN_ID}_body.md\n"
        "NEVER use bash heredocs -- models mangle them into single-line commands.\n"
        "Every response MUST start with: "
        "**Model:** `rescue` **Reasoning:** `high` **Provider:** `rescue`\n"
    )

    rescue_prompt_file.write_text(rescue_prompt)

    # Use a different model than the one that got stuck
    rescue_models = MODEL_CHAIN[1:] if len(MODEL_CHAIN) > 1 else MODEL_CHAIN
    prov, mod, _rescue_reasoning = rescue_models[0]

    log.info("[rescue %s] Spawning rescue agent: %s/%s", rescue_id, prov, mod)

    rescue_toolsets = ("terminal,file,search,web,skills,session_search,todo,"
                       "delegation,vision,image_gen,code_exec,github")

    cmd = [
        HERMES_BIN, "chat",
        "--provider", prov,
        "--model", mod,
        "--reasoning", _rescue_reasoning,
        "--skills", "frameworks-reactive-github,github-auth,github-issues,"
                    "github-pr-workflow,github-code-review,"
                    "caveman,rtk,superpowers,using-superpowers,"
                    "using-git-worktrees,dispatching-parallel-agents,"
                    "verification-before-completion",
        "--toolsets", rescue_toolsets,
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
        if action == "unassigned":
            assignee = payload.get("assignee", {}) or {}
            if assignee.get("login", "").lower() == BOT_USERNAME.lower():
                return {
                    "scope": "issue_unassigned",
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
        if action == "unassigned":
            assignee = payload.get("assignee", {}) or {}
            if assignee.get("login", "").lower() == BOT_USERNAME.lower():
                pr = payload.get("pull_request", {})
                return {
                    "scope": "pr_unassigned",
                    "pr_number": pr.get("number"),
                    "pr_title": pr.get("title", ""),
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
        if action == "review_request_removed":
            reviewer = payload.get("requested_reviewer", {}) or {}
            if reviewer.get("login", "").lower() == BOT_USERNAME.lower():
                pr = payload.get("pull_request", {})
                return {
                    "scope": "pr_review_request_removed",
                    "pr_number": pr.get("number"),
                    "pr_title": pr.get("title", ""),
                    "repo": repo,
                }
        # Merged bot-fork PR → cleanup worktrees + head branch (no Hermes)
        if action == "closed":
            pr = payload.get("pull_request", {}) or {}
            if pr.get("merged") and is_bot_fork_pr(pr):
                head = pr.get("head") or {}
                return {
                    "scope": "pr_merged_cleanup",
                    "pr_number": pr.get("number"),
                    "pr_title": pr.get("title", ""),
                    "pr_branch": head.get("ref", ""),
                    "pr_merged": True,
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
            "pr_title": pr.get("title", ""),
            "comment_body": body,
            "repo": repo,
        }

    if event_type == "discussion":
        # GitHub Discussions fire this for created, edited, answered,
        # and other actions. We treat them like issue comments: the
        # bot responds when mentioned or explicitly asked.
        discussion = payload.get("discussion", {})
        body = discussion.get("body", "") or ""
        title = discussion.get("title", "") or ""
        combined = f"{title} {body}"
        mentions_bot = f"@{BOT_USERNAME}" in combined
        explicit_request = any(
            kw in combined.lower()
            for kw in ["please fix", "please review", "please look", "take a look",
                        "can you", "could you", "needs review",
                        BOT_USERNAME.lower()]
        )
        if not (mentions_bot or explicit_request):
            return None
        return {
            "scope": "discussion",
            "discussion_number": discussion.get("number"),
            "discussion_title": title,
            "discussion_body": body,
            "discussion_category": discussion.get("category", {}).get("name", ""),
            "repo": repo,
        }

    if event_type == "discussion_comment":
        # Comments on a discussion thread. Same trigger logic as
        # issue comments: mention or explicit request.
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
        discussion = payload.get("discussion", {})
        return {
            "scope": "discussion_comment",
            "discussion_number": discussion.get("number"),
            "discussion_title": discussion.get("title", ""),
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

    Reasoning is medium by default. grok-4.5 + high repeatedly hit
    finish_reason=length mid-tool-call on PR #616 (2026-08-30), exited 0,
    and left work incomplete with no fallback.
    """
    is_self_review = (
        classified["scope"] == "pr_review_requested"
        and payload.get("pull_request", {}).get("user", {}).get("login", "").lower()
            == BOT_USERNAME.lower()
    )

    if is_self_review and SELF_REVIEW_MODELS:
        # Deterministic alternation across self-review models
        pr_num = payload.get("pull_request", {}).get("number", 0)
        provider, model, reasoning = SELF_REVIEW_MODELS[
            pr_num % len(SELF_REVIEW_MODELS)]
        return provider, model, reasoning, "1"

    # Default: primary model from chain with its configured reasoning
    provider, model, reasoning = MODEL_CHAIN[0]
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
        "WORKTREE / PARALLEL ISOLATION:",
        "  - This session was started with hermes --worktree. You are already in an",
        f"    isolated git worktree under {WORKTREE_DIR}/hermes-*.",
        "  - Do ALL file edits and commits inside the current worktree cwd.",
        f"  - NEVER mutate the primary checkout at {REPO_PATH} (other parallel agents",
        "    share it). Do not git checkout/pull/reset there.",
        "  - Create feature branches FROM upstream/develop inside this worktree:",
        "      git fetch upstream",
        "      git checkout -B fix/issue-N-slug upstream/develop",
        "    (or chore/... as appropriate). Prefer -B so a leftover local branch is reset.",
        "  - Up to 3 agents run in parallel (relay MAX_CONCURRENT). Stay in your tree.",
        "  - After your PR merges, the relay deletes the head branch + any worktree on it.",
        "    You do NOT need to delete the hermes-* session worktree yourself.",
        "  - NEVER leave the primary tree dirty. NEVER create branches on develop/main.",
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
            "1. Inspect the issue and repo context (stay in current worktree cwd)",
            "2. Create feature branch from upstream/develop IN THIS WORKTREE:",
            "     git fetch upstream && git checkout -B fix/issue-<N>-<slug> upstream/develop",
            "3. Implement the fix",
            "4. Quick checks only (lint/syntax, NOT full builds)",
            "5. GPG-SIGN your commit (git commit -S, ALWAYS)",
            "6. Push to fork (origin), create PR to upstream develop",
            "7. Leave a concise status comment",
            "",
            f"Primary repo path (DO NOT edit): {REPO_PATH}",
            f"Your cwd is already a hermes worktree under {WORKTREE_DIR}/ — work here.",
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
    elif scope == "discussion":
        num = classified.get("discussion_number")
        lines += [
            f"Discussion #{num} mentions @{BOT_USERNAME}",
            f"Title: {classified.get('discussion_title', '')}",
            f"Category: {classified.get('discussion_category', '')}",
            "",
            "Follow Procedure 6 from the skill (treat discussions like issues):",
            "1. Read the discussion and any prior comments",
            "2. Answer or take action as appropriate",
            "3. Include the mandatory prefix",
            "",
            f"Discussion body: {classified.get('discussion_body', '')[:500]}",
            f"Repo is at: {REPO_PATH}",
            f"Use: gh api repos/{ALLOWED_REPO}/discussions/{num}",
        ]
    elif scope == "discussion_comment":
        num = classified.get("discussion_number")
        lines += [
            f"Comment on discussion #{num} mentions @{BOT_USERNAME}",
            f"Title: {classified.get('discussion_title', '')}",
            "",
            "Follow Procedure 6/8 from the skill (treat discussion comments",
            "like issue/PR comments):",
            "1. Read the discussion and prior comments",
            "2. Respond or re-review as appropriate",
            "3. Include the mandatory prefix",
            "",
            f"Comment body: {classified.get('comment_body', '')[:500]}",
            f"Repo is at: {REPO_PATH}",
            f"Use: gh api repos/{ALLOWED_REPO}/discussions/{num}",
        ]

    lines.append("")
    lines.append("Every GitHub response MUST start with this line (bold + code):")
    lines.append(f"  **Model:** `{model}` **Reasoning:** `{reasoning}` **Provider:** `{provider}`")
    lines.append("")
    lines.append("Work in the repo directory. Use gh CLI for all GitHub API calls.")
    lines.append("")
    lines.append("GH BODY RULE: Never use --body with inline text (double quotes mangle")
    lines.append("backticks as command substitution). Instead, use the write_file tool")
    lines.append("to write the review/comment body to a file, then submit with --body-file:")
    lines.append(f"  1. Use write_file to create /tmp/${{SPAWN_ID}}_body.md with the review text")
    lines.append(f"  2. Run: gh pr review NUM --approve --body-file /tmp/${{SPAWN_ID}}_body.md")
    lines.append("NEVER use bash heredocs (cat << 'EOF'). Models often mangle them into")
    lines.append("single-line commands that timeout. Always use write_file + --body-file.")
    lines.append("")
    lines.append("MDX CONTRIBUTOR ATTRIBUTION RULE:")
    lines.append("When creating or editing MDX documentation files, ALWAYS set the YAML")
    lines.append("frontmatter contributors field as follows:")
    lines.append("  contributors:")
    lines.append("    - role: wrote")
    lines.append("      users: [mattaereal]")
    lines.append("    - role: reviewed")
    lines.append("      users: [scode2277]")
    lines.append("NEVER use frameworks-volunteer in the contributors frontmatter.")
    lines.append("")
    lines.append("ABSOLUTE PROHIBITIONS — never do any of the following:")
    lines.append("  - NEVER create test commits, test files, or 'verification' commits.")
    lines.append("  - NEVER commit directly to develop or main. Always use a feature branch.")
    lines.append("  - NEVER pipe output to python3, bash, sh, ruby, node, or any interpreter.")
    lines.append("    (e.g. 'cat file | python3' or 'echo ... | bash' are FORBIDDEN).")
    lines.append("  - If Hermes flags a command as dangerous and denies it, do NOT retry")
    lines.append("    a similar command. Switch to file tools (read_file, search_files).")
    lines.append("  - NEVER run interactive commands that wait for input (nano, vim, less).")
    lines.append("  - NEVER use 'git commit' without '-S' (GPG signing is MANDATORY).")
    lines.append("  - NEVER use the browser tool. It is disabled. Use web_search or")
    lines.append("    web_extract for web content instead.")
    lines.append("  - NEVER use the clarify tool or ask the user questions. You are")
    lines.append("    running autonomously in a one-shot spawn with no human present.")
    lines.append("    Make reasonable default decisions and proceed without asking.")
    lines.append("")
    lines.append("When done, exit. Do not wait for further input.")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Post-exit incomplete workflow detection
# ---------------------------------------------------------------------------

# Scopes that must produce a GitHub review or comment to count as done.
_REVIEW_SCOPES = frozenset({
    "pr_assigned", "pr_review_requested",
})
_COMMENT_SCOPES = frozenset({
    "pr_comment", "issue_comment", "pr_review", "pr_review_comment",
    "discussion", "discussion_comment",
})
_ISSUE_FIX_SCOPES = frozenset({
    "issue_assigned",
})

# Output patterns that mean Hermes aborted without finishing the task.
_TRUNCATION_MARKERS = (
    "finish_reason='length'",
    'finish_reason="length"',
    "Response truncated due to output length limit",
    "Truncated tool call response detected again",
    "refusing to execute incomplete tool arguments",
)


def _scope_action_evidenced(scope: str, output_text: str,
                            completed_actions: set) -> bool:
    """True when the scope's required GitHub action shows evidence of
    having been submitted. Three evidence sources, strongest first:
      1. PTY-guard membership (completed_actions)
      2. An executed (not prompt-echoed) gh command line, excluding
         blocked/denied/errored lines -- mirrors the PTY guard filters
      3. Positive output evidence (API success text / comment URLs)
    Used to suppress false-positive incomplete detection
    (PR #620, 2026-09-03)."""
    exec_marker = chr(0x1F4BB) + " $"  # Hermes executed-command marker

    def _ran(pattern: str) -> bool:
        for ln in output_text.splitlines():
            if exec_marker not in ln:
                continue  # prompt echo, not an executed command
            if "[error]" in ln or "[BLOCKED]" in ln:
                continue
            if "Blocked:" in ln or "denied" in ln.lower():
                continue
            if re.search(pattern, ln):
                return True
        return False

    if scope in _REVIEW_SCOPES:
        if "review" in completed_actions or "pr_comment" in completed_actions:
            return True
        if _ran(r"gh pr review\s+\d+"):
            return True
        return bool(re.search(
            r"(Submitted|approved|requested changes|commented|"
            r"gh pr review.*successfully|Review submitted)",
            output_text,
            re.IGNORECASE,
        ))
    if scope in _COMMENT_SCOPES:
        if ("pr_comment" in completed_actions
                or "issue_comment" in completed_actions):
            return True
        if _ran(r"gh (?:pr|issue) comment\s+\d+"):
            return True
        return bool(re.search(
            r"(gh (?:pr|issue) comment.*successfully|"
            r"https://github\.com/.+/pull/\d+#issuecomment-|"
            r"https://github\.com/.+/issues/\d+#issuecomment-|"
            r"Comment created|commented on)",
            output_text,
            re.IGNORECASE,
        ))
    if scope in _ISSUE_FIX_SCOPES:
        has_pr = bool(re.search(
            r"(gh pr create.*https://github\.com/.+/pull/\d+|"
            r"https://github\.com/.+/pull/\d+|"
            r"pull request created|Created pull request)",
            output_text,
            re.IGNORECASE,
        )) or _ran(r"gh pr create\b.*pull/\d+")
        has_comment = (
            "issue_comment" in completed_actions
            or _ran(r"gh issue comment\s+\d+")
            or bool(re.search(
                r"(gh issue comment.*successfully|"
                r"https://github\.com/.+/issues/\d+#issuecomment-)",
                output_text,
                re.IGNORECASE,
            ))
        )
        return has_pr or has_comment
    return False


def _detect_incomplete_workflow(
    scope: str,
    output_text: str,
    completed_actions: set,
    worktree_dir: str,
) -> list[str]:
    """Return human-readable reasons the spawn looks incomplete, or [].

    Hermes can exit 0 while the workflow is unfinished (PR #616, 2026-08-30):
    output-length truncation loops, unpushed commits left in the worktree,
    or a required review/comment never submitted. Callers treat a non-empty
    result as retryable (return None from spawn_hermes → MODEL_CHAIN fallback).
    """
    reasons: list[str] = []

    # Positive evidence that the scope's required action landed. If it
    # did, a mid-run truncation marker does NOT make the workflow
    # incomplete -- Hermes may truncate and recover, then submit.
    # (PR #620, 2026-09-03: grok-4.5 truncated, recovered, submitted the
    # APPROVE review; the raw marker still caused a chain-wide fallback
    # that re-reviewed the same unchanged PR 3 more times.)
    action_evidenced = _scope_action_evidenced(
        scope, output_text, completed_actions)

    # 1. Output-length truncation / aborted tool-call loops
    trunc_hits = [m for m in _TRUNCATION_MARKERS if m in output_text]
    if trunc_hits and not action_evidenced:
        reasons.append(
            "output truncated/aborted mid-tool-call (%s)"
            % trunc_hits[0]
        )

    # 2. Unpushed commits the BOT authored in THIS spawn's worktree.
    # Only flag when the agent actually EXECUTED `git commit` AND there
    # is no evidence of a successful push. Evidence is restricted to
    # executed-command lines (Hermes prints `💻 $ <cmd>` when a tool
    # runs) -- the PTY transcript also echoes the relay's own prompt,
    # and the prompt contains literal "always use git commit -S"
    # instructions. Scanning the full transcript false-positives every
    # review-only spawn: it never commits and never pushes, so it was
    # flagged "bot committed but did not push" and the chain fell
    # through all 4 models, re-reviewing the same PR each time
    # (PR #620, 2026-09-03).
    try:
        exec_marker = chr(0x1F4BB) + " $"  # Hermes executed-command marker
        executed_lines = [
            ln for ln in output_text.splitlines()
            if exec_marker in ln
        ]
        bot_committed = any(re.search(
            r"\bgit commit\b",
            ln,
        ) for ln in executed_lines)
        push_ok = bool(re.search(
            r"(To https://github\.com/|"
            r"\[[\w./-]+ \w+\.\.\w+\]|"  # [branch abc..def]
            r"git push\b.*(?:successfully|-> ))",
            output_text,
            re.IGNORECASE,
        ))
        hermes_kept_unpushed = (
            "Worktree has unpushed commits, keeping" in output_text
        )
        if bot_committed and not push_ok:
            # Parse this spawn's worktree path for a precise message
            wt_name = None
            m = re.search(
                r"Worktree (?:created|has unpushed commits, keeping):\s*"
                r"(\S+/hermes-[0-9a-f]+)",
                output_text,
            )
            if m:
                wt_name = Path(m.group(1)).name
            # Confirm ahead state when the worktree still exists
            ahead_detail = ""
            if m:
                wt_path = Path(m.group(1))
                if wt_path.is_dir():
                    chk = _run_git(
                        "status", "--porcelain", "--branch",
                        cwd=str(wt_path), timeout=15,
                    )
                    if chk.returncode == 0:
                        for ln in (chk.stdout or "").splitlines():
                            if ln.startswith("##"):
                                ahead_detail = ln.strip()
                                break
            reasons.append(
                "bot committed but did not push"
                + (" in %s" % wt_name if wt_name else "")
                + (" (%s)" % ahead_detail if ahead_detail else "")
                + (" [hermes kept worktree]" if hermes_kept_unpushed else "")
            )
        elif hermes_kept_unpushed and not bot_committed:
            # Informational only — do not treat as incomplete
            log.info(
                "Hermes kept worktree with unpushed commits but bot "
                "did not commit in this spawn -- not flagging incomplete"
            )
    except Exception as e:
        log.debug("incomplete-check worktree scan failed: %s", e)

    # 3. Scope-required GitHub action never submitted
    # completed_actions is populated by the PTY guard when it sees a real
    # `gh pr review N` / `gh pr comment N` / `gh issue comment N` line.
    # action_evidenced covers the same conditions (incl. positive output
    # evidence) -- if the action landed, do not flag incomplete.
    if scope in _REVIEW_SCOPES:
        if not action_evidenced:
            reasons.append(
                "scope %s requires a PR review/comment but none was submitted"
                % scope
            )
    elif scope in _COMMENT_SCOPES:
        if not action_evidenced:
            reasons.append(
                "scope %s requires a comment but none was submitted"
                % scope
            )
    elif scope in _ISSUE_FIX_SCOPES:
        # issue_assigned should produce a PR (or at least a status comment).
        # Detect either a successful `gh pr create` or an issue comment.
        if not action_evidenced:
            # Only flag if the agent actually started work (tools ran) —
            # empty/near-empty spawns are caught by truncation above.
            if "git " in output_text or "gh " in output_text:
                reasons.append(
                    "scope %s produced neither a PR nor a status comment"
                    % scope
                )

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


# ---------------------------------------------------------------------------
# Cross-spawn action dedup (PR #620, 2026-09-03). When a chain falls
# through after the first model ALREADY submitted the required action
# (review/comment), later attempts must not post again. The per-spawn
# PTY guard cannot see sibling spawns; this registry can.
# ---------------------------------------------------------------------------

_cross_spawn_actions: dict = {}  # (scope, repo, number) -> {"models": set()}


def _register_completed_action(scope: str, output_text: str) -> None:
    """If this spawn's log shows the scope's required action landed,
    remember it so fallback attempts on the same target do not re-post."""
    if scope not in (_REVIEW_SCOPES | _COMMENT_SCOPES):
        return
    m = re.search(
        r"gh (?:pr review|pr comment|issue comment)\s+(\d+)"
        r"\s+--repo\s+(\S+)",
        output_text,
    )
    if not m:
        return
    number, repo = m.group(1), m.group(2)
    key = (scope, repo, number)
    _cross_spawn_actions.setdefault(key, {})["posted"] = True
    log.info("Cross-spawn action registered: %s %s #%s -- fallbacks "
             "will not re-post", scope, repo, number)


def _action_already_posted(scope: str, prompt_text: str) -> bool:
    """True when a previous attempt for this same review/comment target
    already posted the required action. The prompt is scanned for the
    canonical fetch command the relay writes into every prompt."""
    if scope not in (_REVIEW_SCOPES | _COMMENT_SCOPES):
        return False
    m = re.search(
        r"gh (?:pr review|pr comment|issue comment)\s+(\d+)"
        r"\s+--repo\s+(\S+)",
        prompt_text,
    )
    if not m:
        return False
    number, repo = m.group(1), m.group(2)
    key = (scope, repo, number)
    entry = _cross_spawn_actions.get(key)
    return bool(entry and entry.get("posted"))


# ---------------------------------------------------------------------------
# Hermes spawner
# ---------------------------------------------------------------------------

def spawn_hermes(prompt: str, provider: str, model: str,
                 scope: str = "", reasoning: str = "medium") -> bool | None:
    """Spawn Hermes in one-shot mode and wait for it to finish.

    Returns:
      True  -- success (workflow complete)
      None  -- retryable failure (rate-limit OR incomplete workflow);
               caller should try the next model in MODEL_CHAIN
      False -- fatal failure (do not retry)

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

    # Toolsets: explicitly whitelist what the agent can use.
    # Browser and clarify are EXCLUDED to prevent prompt injection
    # via web pages and unnecessary waits for user input.
    toolsets = ("terminal,file,search,web,skills,session_search,todo,"
                "delegation,vision,image_gen,code_exec,github")

    cmd = [
        HERMES_BIN,
        "chat",
        "--provider", provider,
        "--model", model,
        "--reasoning", reasoning,
        "--skills", "frameworks-reactive-github,github-auth,github-issues,"
                    "github-pr-workflow,github-code-review,"
                    "caveman,rtk,superpowers,using-superpowers,"
                    "using-git-worktrees,dispatching-parallel-agents,"
                    "verification-before-completion",
        "--toolsets", toolsets,
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
                     "HERMES_REASONING": reasoning,
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
                    if chunk:
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
                        # Use regex with actual numbers so the literal
                        # `gh pr review NUM` in the prompt does NOT match.
                        for action_type, pattern in [
                            ("review", r"gh pr review\s+\d+"),
                            ("pr_comment", r"gh pr comment\s+\d+"),
                            ("issue_comment", r"gh issue comment\s+\d+"),
                        ]:
                            if re.search(pattern, stripped):
                                # Allow retries of failed commands
                                # (e.g. --approve rejected -> retry with --comment).
                                # Skip lines that indicate the command was
                                # blocked, denied, or errored -- these never
                                # reached the GitHub API and should not count
                                # as a completed action.
                                if "[error]" in stripped or "[BLOCKED]" in stripped:
                                    continue
                                if "Blocked:" in stripped or "denied" in stripped.lower():
                                    continue
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

        # Post-exit incomplete-workflow detection (PR #616 lesson).
        # Hermes can exit 0 while the task is unfinished: output-length
        # truncation loops, unpushed commits left in the worktree, or a
        # review/comment never submitted. Treat these as retryable so
        # MODEL_CHAIN falls through instead of logging false "Completed".
        incomplete_reasons = _detect_incomplete_workflow(
            scope=scope,
            output_text=output_text,
            completed_actions=completed_actions,
            worktree_dir=WORKTREE_DIR,
        )
        if incomplete_reasons:
            for reason in incomplete_reasons:
                log.warning("[spawn %s] INCOMPLETE: %s", spawn_id, reason)
            log.warning("[spawn %s] exit=%d but workflow incomplete -- "
                        "will try fallback (session=%s tools=%d)",
                        spawn_id, proc.returncode, session_id, tool_count)
            return None  # None = retry with next model

        log.info("[spawn %s] Done: exit=%d session=%s "
                 "duration=%s msgs=%s tools=%d output=%s",
                 spawn_id, proc.returncode, session_id,
                 duration, msg_count, tool_count, log_file.name)
        # Record that this spawn's required action landed so chain
        # fallbacks on the same target do not re-post (PR #620).
        _register_completed_action(scope, output_text)
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
                            "pull_request_review_comment",
                            "discussion", "discussion_comment"):
            sender = payload.get("sender", {}).get("login", "")
        elif event_type == "push":
            sender = payload.get("sender", {}).get("login", "")

        if sender.lower() == BOT_USERNAME.lower():
            log.info("Event from bot (%s) -- ignoring", sender)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"self-event")
            return

        # 7. Enforce whitelist (exception: merged bot-fork PR cleanup)
        action = payload.get("action", "")
        if sender and sender.lower() not in ALLOWED_SENDERS:
            # Allow post-merge cleanup of our own fork heads regardless of
            # who clicked Merge (maintainers may not be on ALLOWED_SENDERS).
            if event_type == "pull_request" and action == "closed":
                pr = payload.get("pull_request", {}) or {}
                if pr.get("merged") and is_bot_fork_pr(pr):
                    stats = cleanup_after_merged_pr(pr)
                    log.info(
                        "pr_merged_cleanup (non-whitelist merger=%s) PR #%s stats=%s",
                        sender, pr.get("number"), stats,
                    )
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"cleaned (non-whitelist merger)")
                    return
            log.info("Sender %s not in whitelist -- ignoring", sender)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"not whitelisted")
            return

        # 8. Classify
        classified = classify_event(event_type, action, payload)
        if classified is None:
            log.info("Event %s/%s not in scope -- ignoring", event_type, action)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"not in scope")
            return

        # Handle unassignment / review request removal -- cancel queued work
        if classified["scope"] in (
            "issue_unassigned", "pr_unassigned", "pr_review_request_removed"
        ):
            removed = cancel_pending_work(classified)
            num = classified.get("issue_number") or classified.get("pr_number")
            log.info("Processed cancellation: %s #%s (removed %d queued items)",
                     classified["scope"], num, removed)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"cancelled ({removed} items)".encode())
            return

        # Merged bot PR: delete worktree(s) + local/remote head branch.
        # No Hermes spawn. Runs for any merger (whitelist already passed OR
        # we re-check bot head so a non-whitelist merge still cleans up —
        # see early path below if whitelist blocked).
        if classified["scope"] == "pr_merged_cleanup":
            pr = payload.get("pull_request", {}) or {}
            stats = cleanup_after_merged_pr(pr)
            log.info("pr_merged_cleanup PR #%s branch=%s stats=%s",
                     classified.get("pr_number"), stats.get("branch"), stats)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                f"cleaned worktrees={stats.get('worktrees')} "
                f"local={stats.get('local_branch')} "
                f"remote={stats.get('remote_branch')}".encode()
            )
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
        # Rough busy count: how many semaphore slots free
        try:
            busy = MAX_CONCURRENT - concurrency_sem._value  # noqa: SLF001
        except Exception:
            busy = "?"
        log.info("Enqueued: scope=%s (queue depth: %d, busy slots: %s/%d)",
                 classified["scope"], queue_depth + 1, busy, MAX_CONCURRENT)

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
    # Reap leftover hermes session worktrees from prior crashes
    try:
        stats = prune_stale_hermes_worktrees()
        log.info("Startup worktree prune: %s", stats)
    except Exception as e:
        log.warning("Startup worktree prune failed: %s", e)
    # Background reaper (hourly by default)
    threading.Thread(target=worktree_reaper_loop, daemon=True).start()
    server = HTTPServer(("127.0.0.1", RELAY_PORT), WebhookHandler)
    log.info("Relay listening on 127.0.0.1:%d", RELAY_PORT)
    log.info("Repo: %s  Bot: %s  Whitelist: %s",
             ALLOWED_REPO, BOT_USERNAME, ALLOWED_SENDERS)
    log.info("Parallelism: MAX_CONCURRENT=%d (each spawn uses hermes --worktree)",
             MAX_CONCURRENT)
    log.info("Worktrees: dir=%s cleanup_on_merge=%s stale_hours=%d",
             WORKTREE_DIR, CLEANUP_ON_MERGE, STALE_WORKTREE_HOURS)
    log.info("Model chain: %s",
             " -> ".join(f"{p}/{m}@{r}" for p, m, r in MODEL_CHAIN))
    if SELF_REVIEW_MODELS:
        log.info("Self-review models: %s",
                 " -> ".join(f"{p}/{m}@{r}" for p, m, r in SELF_REVIEW_MODELS))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.server_close()

if __name__ == "__main__":
    main()
