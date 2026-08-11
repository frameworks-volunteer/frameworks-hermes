#!/usr/bin/env python3
"""
Verify relay.py changes after model chain, toolset, or event-type edits.
Run: python3 scripts/verify-relay.py [--relay-path PATH]

Checks:
  1. relay.py parses as valid Python
  2. config.env has the expected model chain slugs
  3. classify_event handles all expected event types
  4. build_prompt produces prompts for each scope
  4b. false-positive keyword trigger (PR #529 lesson)
  5. --toolsets flag present in spawn commands
  6. browser and clarify excluded from toolsets
  7. sender extraction includes all event types
  8. prompt text contains browser/clarify prohibitions
  9. No stale model names in SKILL.md or config.env

Exit 0 = all pass, exit 1 = failures.
"""
import ast
import os
import sys
import importlib.util

RELAY_DEFAULT = os.path.expanduser("~/ops/frameworks-gh-relay/relay.py")
CFG_DEFAULT   = os.path.expanduser("~/ops/frameworks-gh-relay/config.env")
SOUL_DEFAULT  = os.path.expanduser("~/.hermes/SOUL.md")
SKILL_DEFAULT = os.path.expanduser("~/.hermes/skills/github/frameworks-reactive-github/SKILL.md")

RELAY = sys.argv[sys.argv.index("--relay-path") + 1] if "--relay-path" in sys.argv else RELAY_DEFAULT
CFG   = CFG_DEFAULT
SOUL  = SOUL_DEFAULT
SKILL = SKILL_DEFAULT

errors = []
passed = 0

def check(desc, cond):
    global passed
    if cond:
        passed += 1
        print(f"  PASS: {desc}")
    else:
        errors.append(desc)
        print(f"  FAIL: {desc}")

# 1. Syntax
print("=== 1. relay.py syntax ===")
with open(RELAY) as f:
    src = f.read()
try:
    ast.parse(src)
    check("AST parse OK", True)
except SyntaxError as e:
    check(f"AST parse ({e})", False)
    sys.exit(1)  # can't continue

# 2. config.env
print("\n=== 2. config.env ===")
with open(CFG) as f:
    cfg = f.read()
for slug in ["z-ai/glm-5.2", "moonshotai/kimi-k2.6", "deepseek/deepseek-v4-flash"]:
    check(f"config.env has {slug}", slug in cfg)
for old in ["glm-5.1", "MiniMax-M2.7", "kimi-k2.5"]:
    check(f"config.env: no {old}", old not in cfg)

# 3. Load module and test classify_event
print("\n=== 3. classify_event (live test) ===")
spec = importlib.util.spec_from_file_location("relay", RELAY)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

BOT = "frameworks-volunteer"
REPO = "security-alliance/frameworks"

test_events = [
    ("discussion", {"action": "created", "discussion": {"number": 1, "title": "T", "body": f"@{BOT} help", "category": {"name": "Q&A"}}, "repository": {"full_name": REPO}}, "discussion", True),
    ("discussion", {"action": "created", "discussion": {"number": 2, "title": "T", "body": "no mention", "category": {}}, "repository": {"full_name": REPO}}, None, True),
    ("discussion_comment", {"action": "created", "comment": {"body": f"@{BOT} check"}, "discussion": {"number": 1, "title": "T"}, "repository": {"full_name": REPO}}, "discussion_comment", True),
    ("issues", {"action": "assigned", "assignee": {"login": BOT}, "issue": {"number": 99, "title": "Test"}, "repository": {"full_name": REPO}}, "issue_assigned", True),
    ("pull_request", {"action": "assigned", "assignee": {"login": BOT}, "pull_request": {"number": 5, "title": "T", "user": {"login": "someone"}}, "repository": {"full_name": REPO}}, "pr_assigned", True),
    ("pull_request", {"action": "review_requested", "requested_reviewer": {"login": BOT}, "pull_request": {"number": 6, "title": "T", "user": {"login": "someone"}}, "repository": {"full_name": REPO}}, "pr_review_requested", True),
    ("issue_comment", {"action": "created", "comment": {"body": f"@{BOT} fix"}, "issue": {"number": 10}, "repository": {"full_name": REPO}}, "issue_comment", True),
    ("issue_comment", {"action": "created", "comment": {"body": f"@{BOT} fix"}, "issue": {"number": 10, "pull_request": {}}, "repository": {"full_name": REPO}}, "pr_comment", True),
    # Merged bot-fork PR → cleanup scope (no Hermes)
    ("pull_request", {
        "action": "closed",
        "pull_request": {
            "number": 42,
            "title": "fix: x",
            "merged": True,
            "user": {"login": BOT},
            "head": {
                "ref": "fix/issue-42-slug",
                "user": {"login": BOT},
                "repo": {"full_name": f"{BOT}/frameworks"},
            },
        },
        "repository": {"full_name": REPO},
    }, "pr_merged_cleanup", True),
    # Closed without merge → ignore
    ("pull_request", {
        "action": "closed",
        "pull_request": {
            "number": 43,
            "title": "fix: y",
            "merged": False,
            "user": {"login": BOT},
            "head": {
                "ref": "fix/issue-43-slug",
                "user": {"login": BOT},
                "repo": {"full_name": f"{BOT}/frameworks"},
            },
        },
        "repository": {"full_name": REPO},
    }, None, True),
    # Merged but NOT bot fork → ignore
    ("pull_request", {
        "action": "closed",
        "pull_request": {
            "number": 44,
            "title": "fix: z",
            "merged": True,
            "user": {"login": "other"},
            "head": {
                "ref": "feature",
                "user": {"login": "other"},
                "repo": {"full_name": "other/frameworks"},
            },
        },
        "repository": {"full_name": REPO},
    }, None, True),
]

for etype, payload, expect_scope, should_pass in test_events:
    result = mod.classify_event(etype, payload.get("action", ""), payload)
    action = payload.get("action", "")
    label = f"{etype}/{action}"
    if expect_scope is None:
        check(f"{label} -> None", result is None)
    else:
        check(f"{label} -> {expect_scope}", result is not None and result.get("scope") == expect_scope)

# 4. build_prompt
print("\n=== 4. build_prompt (live test) ===")
for etype, payload, expect_scope, _ in test_events:
    if expect_scope is None:
        continue
    result = mod.classify_event(etype, payload.get("action", ""), payload)
    if result is None:
        continue
    if expect_scope == "pr_merged_cleanup":
        # cleanup is handled in-process; no Hermes prompt required
        check("is_bot_fork_pr true for merged bot head",
              mod.is_bot_fork_pr(payload["pull_request"]))
        continue
    p = mod.build_prompt(result, "openrouter", "test-model", "medium", "0", payload)
    check(f"prompt for {expect_scope} non-empty", len(p) > 100)
    check(f"prompt for {expect_scope} has mandatory prefix line", "Model:" in p and "Reasoning:" in p and "Provider:" in p)
    if expect_scope == "issue_assigned":
        check("issue_assigned prompt has worktree isolation", "WORKTREE" in p or "worktree" in p)
        check("issue_assigned branches from upstream/develop", "upstream/develop" in p)

# 4b. False-positive keyword test (PR #529 lesson)
print("\n=== 4b. false-positive keyword trigger ===")
# "can you" without @mention SHOULD ideally not trigger, but currently does.
# Document the current behavior so the test catches regressions if the
# keyword list is narrowed.
fp_payload = {
    "action": "created",
    "comment": {"body": "Can you try again and let me know?"},
    "issue": {"number": 999},
    "repository": {"full_name": REPO},
}
fp_result = mod.classify_event("issue_comment", "created", fp_payload)
# Currently triggers (known issue). If this changes to None, update the test.
check("issue_comment 'can you' without @mention currently triggers (known false-positive)", fp_result is not None)

# 5. toolsets
print("\n=== 5. toolsets ===")
check("--toolsets in relay.py", "--toolsets" in src)
check("browser excluded", "browser" not in src.split("toolsets = (")[1].split(")")[0] if "toolsets = (" in src else False)
check("clarify excluded", "clarify" not in src.split("toolsets = (")[1].split(")")[0] if "toolsets = (" in src else False)
check("rescue has --toolsets", "rescue_toolsets" in src)

# 6. sender extraction
print("\n=== 6. sender extraction ===")
sender_blk = src[src.find('elif event_type in ("issue_comment"'):src.find('elif event_type == "push"')]
for etype in ["discussion", "discussion_comment", "issue_comment", "pull_request_review", "pull_request_review_comment"]:
    check(f"{etype} in sender extraction", f'"{etype}"' in sender_blk)

# 7. prompt prohibitions
print("\n=== 7. prompt prohibitions ===")
check("browser prohibition", "NEVER use the browser tool" in src)
check("clarify prohibition", "NEVER use the clarify tool" in src)
check("heredoc prohibition", "NEVER use bash heredocs" in src)
check("no heredoc cat in prompt", "cat > /tmp/${SPAWN_ID}_body.md << 'EOF'" not in src)
check("write_file instruction in prompt", "use the write_file tool" in src)
guard_section = src[src.find("Detect duplicate"):src.find("Log Hermes responses")] if "Detect duplicate" in src and "Log Hermes responses" in src else ""
check("[BLOCKED] in duplicate guard skip", "[BLOCKED]" in guard_section)
check("Blocked: in duplicate guard skip", "Blocked:" in guard_section)
check("denied in duplicate guard skip", "denied" in guard_section.lower())

# 8. SOUL.md
print("\n=== 8. SOUL.md ===")
with open(SOUL) as f:
    soul = f.read()
check("SOUL has discussion row", "| discussion" in soul)
check("SOUL has discussion_comment row", "| discussion_comment" in soul)

# 9. SKILL.md no stale models
print("\n=== 9. SKILL.md ===")
with open(SKILL) as f:
    skill = f.read()
for slug in ["glm-5.2", "kimi-k2.6", "deepseek-v4-flash"]:
    check(f"SKILL has {slug}", slug in skill)
for old in ["glm-5.1", "MiniMax-M2.7", "kimi-k2.5"]:
    check(f"SKILL: no {old}", old not in skill)
check("SKILL has Procedure 9", "Procedure 9" in skill)
check("SKILL has --toolsets", "--toolsets" in skill)

# 10. Worktree parallelism
print("\n=== 10. worktree parallelism ===")
check("--worktree in spawn_hermes cmd", '"--worktree"' in src)
check("MAX_CONCURRENT default 3", 'MAX_CONCURRENT' in src and '"3"' in src)
check("cleanup_after_merged_pr defined", "def cleanup_after_merged_pr" in src)
check("prune_stale_hermes_worktrees defined", "def prune_stale_hermes_worktrees" in src)
check("pr_merged_cleanup scope string", "pr_merged_cleanup" in src)
check("worktree_reaper_loop defined", "def worktree_reaper_loop" in src)
check("CLEANUP_ON_MERGE config", "CLEANUP_ON_MERGE" in src)

# Summary
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {len(errors)} failed")
if errors:
    print(f"FAILED: {', '.join(errors)}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
