#!/usr/bin/env python3
"""Weekly dependabot security check for security-alliance/frameworks.

Design goals (after 14 weeks of consecutive failures):
1. NEVER touch the main working tree / local develop branch.
   Parallel reactive sessions may own ~/frameworks; this script works in
   a throwaway git worktree instead.
2. Stay under the Hermes no_agent cron timeout (120s) when possible:
   - fail-fast existing-PR check first
   - narrow duplicate guard (security-fix titles only)
   - reuse main-repo node_modules via symlink
   - lockfile-only install
3. Silent (no stdout) when nothing to do (watchdog pattern).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

REPO = "/home/zealot/frameworks"
UPSTREAM = "security-alliance/frameworks"
FORK_REMOTE = "origin"
WORKTREE_ROOT = "/tmp/fw-dependabot-cron"
GIT_USER = "frameworks-volunteer"
GIT_EMAIL = "266408623+frameworks-volunteer@users.noreply.github.com"

# Match only real weekly security-fix PRs, not config PRs like
# "chore(deps): add Dependabot configuration".
SECURITY_PR_RE = re.compile(
    r"fix\(security\).*dependabot|dependabot.*security",
    re.IGNORECASE,
)


def shell(cmd, cwd=REPO, check=True, timeout=90, env=None):
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    r = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=run_env,
    )
    if check and r.returncode != 0:
        print(f"CMD FAILED: {cmd}\nSTDERR: {r.stderr}\nSTDOUT: {r.stdout}")
        sys.exit(1)
    return r


def extract_json(text):
    """Grab first JSON object from mixed output."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    return json.loads(text[start : end + 1])


def cleanup_worktree():
    """Best-effort remove of previous throwaway worktree/branch state."""
    # Remove registered worktree if present
    shell(
        f'git worktree remove --force "{WORKTREE_ROOT}" 2>/dev/null || true',
        check=False,
        timeout=30,
    )
    if os.path.isdir(WORKTREE_ROOT):
        shutil.rmtree(WORKTREE_ROOT, ignore_errors=True)
    # Prune stale worktree metadata
    shell("git worktree prune", check=False, timeout=30)


def main():
    # 1. Fail-fast: existing open security-fix PR from this bot
    existing = shell(
        f"gh pr list --repo {UPSTREAM} --author frameworks-volunteer "
        f"--json number,title,headRefName --state open",
        check=False,
        timeout=30,
    )
    if existing.returncode == 0:
        try:
            prs = json.loads(existing.stdout)
            for pr in prs:
                title = pr.get("title", "")
                head = pr.get("headRefName", "")
                if SECURITY_PR_RE.search(title) or head.startswith(
                    "fix/dependabot-weekly-"
                ):
                    print(
                        f"Existing dependabot security PR already open: "
                        f"#{pr['number']} ({title})"
                    )
                    sys.exit(0)
        except Exception:
            pass

    # 2. Prepare isolated worktree from upstream/develop
    #    Never checkout/reset the main working tree.
    cleanup_worktree()

    # Drop accidental local branch that shadows the remote-tracking ref
    local_upstream = shell("git branch --list upstream/develop", check=False, timeout=15)
    if local_upstream.stdout.strip():
        shell("git update-ref -d refs/heads/upstream/develop", check=False, timeout=15)
        print("Deleted stale local upstream/develop branch")

    shell("git fetch upstream develop", timeout=60)

    dt = datetime.now().strftime("%Y%m%d")
    branch = f"fix/dependabot-weekly-{dt}"

    # Clean stale same-day branch (local + origin) from prior failed runs
    local_branches = shell("git branch --list", check=False, timeout=15)
    if re.search(rf"(^|\s){re.escape(branch)}(\s|$)", local_branches.stdout):
        shell(f"git branch -D {branch}", check=False, timeout=15)
        print(f"Deleted stale local branch: {branch}")
    remote_branches = shell(
        f"git branch -r --list {FORK_REMOTE}/{branch}", check=False, timeout=15
    )
    if remote_branches.stdout.strip():
        shell(f"git push {FORK_REMOTE} --delete {branch}", check=False, timeout=30)
        print(f"Deleted stale remote branch: {FORK_REMOTE}/{branch}")

    # Create throwaway worktree at upstream/develop on the feature branch
    shell(
        f'git worktree add -b {branch} "{WORKTREE_ROOT}" upstream/develop',
        timeout=30,
    )
    wt = WORKTREE_ROOT

    try:
        # Reuse installed deps from the main clone so pnpm does not re-resolve
        # the world. lockfile-only still needs a package manager + store hints.
        main_nm = os.path.join(REPO, "node_modules")
        wt_nm = os.path.join(wt, "node_modules")
        if os.path.isdir(main_nm) and not os.path.exists(wt_nm):
            os.symlink(main_nm, wt_nm)

        # 3. Audit (JSON only; stderr discarded)
        audit = subprocess.run(
            "npx --yes pnpm audit --json",
            shell=True,
            cwd=wt,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=60,
        )

        if audit.returncode == 0:
            # clean — silent cron
            sys.exit(0)

        try:
            data = extract_json(audit.stdout or "")
        except Exception:
            print("Failed to parse audit JSON")
            print(audit.stdout)
            sys.exit(1)

        if not data:
            sys.exit(0)

        advisories = data.get("advisories", {})
        if not advisories:
            sys.exit(0)

        print(f"Found {len(advisories)} open security advisories")

        # 4. Build overrides from advisories + actions
        overrides = {}
        for _adv_id, adv in advisories.items():
            name = adv.get("module_name")
            pv = adv.get("patched_versions", "")
            if name and pv:
                overrides[name] = pv

        for action in data.get("actions", []):
            name = action.get("module")
            target = action.get("target")
            if name and target and name in overrides:
                overrides[name] = f">={target}"

        if not overrides:
            print("No actionable patched versions found.")
            sys.exit(0)

        # 5. Write package.json overrides
        pkg_path = os.path.join(wt, "package.json")
        with open(pkg_path) as f:
            pkg = json.load(f)

        pkg.setdefault("pnpm", {})
        pkg["pnpm"].setdefault("overrides", {})
        existing_overrides = pkg["pnpm"]["overrides"]

        changed = False
        for name, version in overrides.items():
            if existing_overrides.get(name) != version:
                existing_overrides[name] = version
                changed = True

        if not changed:
            print("Overrides already up to date; no lockfile changes needed.")
            sys.exit(0)

        with open(pkg_path, "w") as f:
            json.dump(pkg, f, indent=2)
            f.write("\n")

        # 6. Regenerate lockfile only (no node_modules install)
        shell("npx --yes pnpm install --lockfile-only", cwd=wt, timeout=75)

        status = shell("git status --short", cwd=wt, timeout=15)
        if not status.stdout.strip():
            print("No lockfile changes after install.")
            sys.exit(0)

        # 7. Commit (GPG-signed) + push + PR
        shell("git add package.json pnpm-lock.yaml", cwd=wt, timeout=15)
        commit_env = {
            "GIT_AUTHOR_NAME": GIT_USER,
            "GIT_AUTHOR_EMAIL": GIT_EMAIL,
            "GIT_COMMITTER_NAME": GIT_USER,
            "GIT_COMMITTER_EMAIL": GIT_EMAIL,
        }
        shell(
            'git commit -S -m "fix(security): weekly dependabot security updates"',
            cwd=wt,
            timeout=30,
            env=commit_env,
        )
        shell(f"git push -u {FORK_REMOTE} {branch}", cwd=wt, timeout=45)

        body_lines = [
            f"## Weekly Dependabot Security Update ({dt})",
            "",
            f"Automated fix for {len(advisories)} open security advisory/advisories.",
            "",
            "### Fixed packages",
        ]
        for _adv_id, adv in advisories.items():
            body_lines.append(
                f"- **{adv.get('module_name', '?')}**: "
                f"{adv.get('patched_versions', '')} "
                f"({adv.get('github_advisory_id', 'N/A')})"
            )
        body_lines.append("")
        body_lines.append("Closes open dependabot alerts.")
        body = "\n".join(body_lines)

        body_file = f"/tmp/pr_body_dependabot_{dt}.md"
        with open(body_file, "w") as f:
            f.write(body)

        shell(
            f"gh pr create --repo {UPSTREAM} "
            f"--head frameworks-volunteer:{branch} "
            f"--base develop "
            f'--title "fix(security): weekly dependabot security updates ({dt})" '
            f"--body-file {body_file}",
            cwd=wt,
            timeout=30,
        )
        print("PR created successfully.")
    finally:
        # Always detach worktree so main repo stays clean for reactive agents.
        cleanup_worktree()
        # Keep the branch ref only on origin; drop local branch after push.
        shell(f"git branch -D {branch}", check=False, timeout=15)


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired as e:
        print(f"CMD TIMED OUT: {e.cmd} (timeout={e.timeout}s)")
        cleanup_worktree()
        sys.exit(1)
    except SystemExit:
        # Ensure worktree cleanup on intentional exits after worktree creation.
        # (early exits before worktree are no-ops)
        if os.path.isdir(WORKTREE_ROOT):
            cleanup_worktree()
        raise
    except Exception as e:
        print(f"UNEXPECTED ERROR: {e}")
        cleanup_worktree()
        sys.exit(1)
