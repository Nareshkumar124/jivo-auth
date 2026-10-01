#!/usr/bin/env python3
"""
Scan everything a `git push` would publish for secrets and server details.

The repository is public, so this looks at what would become public next:

* lines added by uncommitted changes (staged, unstaged, and untracked files),
* lines added by local commits that aren't on the upstream branch yet.

Only added lines are checked, so content that's already public doesn't
flag again on every run.

Server patterns aren't written here, because this file is public too. They
come from the gitignored deploy.env: the user and host in DEPLOY_HOST,
DEPLOY_DIR, and an optional LEAK_SCAN_EXTRA regex for anything else private
(internal IPs, role names, admin emails).

Exit status: 0 = clean, 1 = findings to review, 2 = couldn't run.
"""

import re
import subprocess
import sys
from pathlib import Path

# Files that must never be committed, whatever their content.
FORBIDDEN_PATHS = re.compile(
    r"(^|/)\.env(\.(?!example$)[^/]+)?$|(^|/)(deploy\.env|DEPLOYMENT\.md)$"
    r"|\.(pem|key|dump|sqlite3)$|(^|/)id_(rsa|ed25519|ecdsa)"
)

SECRET_PATTERNS = {
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "Slack token": r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    "JWT": r"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{20,}",
    "credential assignment": (
        r"(?i)\b[A-Z0-9_]*(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)[A-Z0-9_]*"
        r"\s*[=:]\s*['\"]?[A-Za-z0-9+/_\-!@#$%^&*.]{16,}"
    ),
    "password in URL": r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{6,}@",
}

# Known placeholders and test values that look like secrets but aren't.
ALLOWED_SUBSTRINGS = (
    "collectstatic-only-not-a-real-secret",
    "django-insecure-development-key",
    "jivo-auth-client-tests",
    "at-least-32-random-characters",
    "change-me",
    "<stored securely",
)


def git(*args):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    ).stdout


def read_deploy_env(root):
    values = {}
    path = root / "deploy.env"

    if not path.exists():
        return values

    for raw in path.read_text().splitlines():
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")

    return values


def server_patterns(env):
    patterns = {}
    host = env.get("DEPLOY_HOST", "")

    if "@" in host:
        user, host = host.split("@", 1)
        patterns["server SSH user"] = rf"\b{re.escape(user)}\b"

    if host:
        patterns["server host"] = re.escape(host)

    if env.get("DEPLOY_DIR"):
        patterns["server path"] = re.escape(env["DEPLOY_DIR"].rstrip("/"))

    if env.get("LEAK_SCAN_EXTRA"):
        patterns["private detail (LEAK_SCAN_EXTRA)"] = env["LEAK_SCAN_EXTRA"]

    return patterns


def added_lines_from_diff(diff_text):
    """Yield (path, new line number, text) for every added line in a diff."""

    path, line_no = None, 0

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            line_no = int(match.group(1)) if match else 0
        elif line.startswith("+") and path:
            yield path, line_no, line[1:]
            line_no += 1
        elif not line.startswith("-"):
            line_no += 1


def mask(text, match):
    secret = match.group(0)
    shown = secret[:6] + "…" if len(secret) > 10 else secret
    return (text[: match.start()] + shown + text[match.end():]).strip()[:160]


def main():
    root_text = git("rev-parse", "--show-toplevel").strip()

    if not root_text:
        print("leak_scan: not inside a git repository", file=sys.stderr)
        return 2

    root = Path(root_text)
    env = read_deploy_env(root)
    patterns = {**SECRET_PATTERNS, **server_patterns(env)}

    if "server host" not in patterns:
        print("WARNING: no deploy.env DEPLOY_HOST, so server details can't be checked.")

    compiled = {name: re.compile(p) for name, p in patterns.items()}
    lines = []
    paths = set()      # every path the push would touch
    published = set()  # paths whose content the push would carry

    # Uncommitted changes to tracked files (staged and unstaged). A path that
    # is still in the index gets committed; one removed from it doesn't.
    head_exists = bool(git("rev-parse", "--verify", "-q", "HEAD").strip())
    base = "HEAD" if head_exists else "--cached"
    lines += added_lines_from_diff(git("diff", "--unified=0", "--no-color", base))
    changed = set(git("diff", "--name-only", base).splitlines())
    paths |= changed
    published |= changed & set(git("ls-files").splitlines())

    # Untracked files that would be added by `git add -A`.
    for rel in git("ls-files", "--others", "--exclude-standard").splitlines():
        paths.add(rel)
        published.add(rel)
        file = root / rel

        try:
            data = file.read_bytes()
        except OSError:
            continue

        if b"\0" in data[:8192]:
            continue

        for no, text in enumerate(data.decode(errors="replace").splitlines(), 1):
            lines.append((rel, no, text))

    # Local commits the push would publish.
    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip()
    unpushed = git("rev-list", f"{upstream}..HEAD").split() if upstream else []

    if unpushed:
        lines += added_lines_from_diff(
            git("log", "-p", "--unified=0", "--no-color", "--format=", f"{upstream}..HEAD")
        )
        paths |= set(git("log", "--name-only", "--format=", f"{upstream}..HEAD").splitlines())
        published |= set(
            git("log", "--diff-filter=ACMR", "--name-only", "--format=", f"{upstream}..HEAD").splitlines()
        )

    findings = [
        f"{rel}: FORBIDDEN FILE (must never be committed)"
        for rel in sorted(published)
        if rel and FORBIDDEN_PATHS.search(rel)
    ]

    for rel, no, text in lines:
        if any(allowed in text for allowed in ALLOWED_SUBSTRINGS):
            continue

        for name, regex in compiled.items():
            match = regex.search(text)

            if match:
                findings.append(f"{rel}:{no}: {name}: {mask(text, match)}")
                break

    scope = f"{len(paths)} changed file(s)"

    if unpushed:
        scope += f", {len(unpushed)} unpushed commit(s)"

    if findings:
        print(f"Leak scan: {len(findings)} finding(s) in {scope}. Review each one:")
        print("\n".join(f"  {f}" for f in findings))
        return 1

    print(f"Leak scan: clean ({scope}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
