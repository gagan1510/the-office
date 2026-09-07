"""Read-only GitHub PR checks and conservative, evidence-based next steps."""
import json
import re
import subprocess


def inspect_pr(url, gh, environment):
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9]\d*)/?", url)
    if not match:
        raise ValueError("Use a github.com pull-request URL.")
    if not gh:
        raise ValueError("GitHub CLI (gh) must be installed and authenticated to monitor checks.")
    repository, number = match.groups()

    def read(*args):
        result = subprocess.run([gh, *args], capture_output=True, text=True,
                                timeout=30, env=environment, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[:2000] or "GitHub checks unavailable.")
        return result.stdout

    pr = json.loads(read("pr", "view", number, "--repo", repository, "--json",
                         "title,state,headRefOid,statusCheckRollup"))
    checks = []
    for item in pr.get("statusCheckRollup") or []:
        status = item.get("conclusion") or item.get("state") or item.get("status") or "UNKNOWN"
        checks.append({"name": item.get("name") or item.get("context") or "Check",
                       "status": status, "url": item.get("detailsUrl") or item.get("targetUrl") or ""})
    failures = [item for item in checks if item["status"] in
                {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}]
    pending = any(item["status"] not in {"SUCCESS", "NEUTRAL", "SKIPPED"} for item in checks)
    status = "failed" if failures else "pending" if pending else "passing" if checks else "waiting"
    evidence, errors, seen = [], [], set()
    for item in failures:
        run = re.match(r"https://github\.com/" + re.escape(repository) + r"/actions/runs/(\d+)(?:/|$)", item["url"])
        if not run or run[1] in seen or len(seen) >= 2:
            continue
        seen.add(run[1])
        try:
            evidence.append(read("run", "view", run[1], "--repo", repository, "--log-failed")[-12000:])
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc)[:2000])
    logs = '\n'.join(evidence)
    suggestions = []
    if failures:
        lower = logs.lower()
        if any(word in lower for word in ("eslint", "ruff", "prettier", "formatting")):
            suggestions.append("Fix the reported lint/format violations in the cited files, then rerun the same CI command.")
        if any(word in lower for word in ("assertionerror", "assertion failed", "failed test", "expected:")):
            suggestions.append("Reproduce the failing assertions at this PR commit; fix the behavior or outdated fixtures and add regression coverage without weakening the tests.")
        if any(word in lower for word in ("modulenotfounderror", "cannot find module", "no matching distribution")):
            suggestions.append("Check dependency declarations, lockfiles and CI installation steps for the missing module; align CI with the project's supported runtime.")
        if not suggestions:
            suggestions.append("Inspect the linked failed jobs, reproduce the failing CI command, and determine whether the cause is code, configuration or runner infrastructure before changing files.")
    return {"url": url, "title": pr.get("title", ""), "state": pr.get("state"),
            "head": pr.get("headRefOid"), "status": status, "checks": checks,
            "failures": failures, "evidence": logs, "logErrors": errors,
            "suggestions": suggestions, "suggestionNote": "Suggested next steps, not a verified root-cause diagnosis."}
