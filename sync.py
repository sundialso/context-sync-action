#!/usr/bin/env python3
"""Sync playbook markdown files to the Sundial Context Engine push endpoint.

See action.yml for the env contract. Stdlib only.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API_URL = os.environ["API_URL"].rstrip("/")
TOKEN = os.environ["TOKEN"]
INCLUDE = [p.strip() for p in os.environ["INCLUDE"].splitlines() if p.strip()]
EVENT = os.environ.get("GITHUB_EVENT_NAME", "")


def _reject_pattern(pattern: str, reason: str) -> None:
    # Reject malformed `include` patterns up front. If we let them through,
    # the upsert path (glob.glob) and the delete path (git diff filter) see
    # different forms of the same file (one with `./` prefix, one without)
    # and the action silently misses deletes — i.e. orphans playbooks on the
    # server with no way to clean them up. A loud failure is much better
    # than that.
    print(
        f"::error::Invalid include pattern {pattern!r}: {reason}. "
        f"Use a plain repo-relative glob like 'playbooks/**/*.md'.",
        file=sys.stderr,
    )
    sys.exit(1)


for _p in INCLUDE:
    if _p.startswith("./"):
        _reject_pattern(_p, "leading './' is not supported")
    elif _p.startswith("/"):
        _reject_pattern(_p, "absolute paths are not supported")
    elif _p.startswith("..") or ".." in _p.split("/"):
        _reject_pattern(_p, "'..' segments are not supported")
    elif "\\" in _p:
        _reject_pattern(_p, "backslashes are not supported (use forward slashes)")
BATCH = 10
MAX_BYTES = 256 * 1024
HTTP_TIMEOUT_S = 30
ZERO_SHA = "0" * 40


def fail(msg: str) -> None:
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


# --- BEFORE/AFTER + dry-run mode from the event ---

if EVENT == "push":
    BEFORE = os.environ.get("EVENT_BEFORE", "")
    AFTER = os.environ.get("EVENT_AFTER", "")
elif EVENT in ("pull_request", "pull_request_target"):
    BEFORE = os.environ.get("PR_BASE_SHA", "")
    AFTER = os.environ.get("PR_HEAD_SHA", "")
else:
    fail(f"Unsupported event '{EVENT}'. Use push or pull_request.")

if BEFORE == ZERO_SHA:
    BEFORE = ""

_dry = os.environ.get("DRY_RUN", "").strip().lower()
DRY_RUN = _dry == "true" or (_dry == "" and EVENT.startswith("pull_request"))


# --- Include glob match / expand ---

def _glob_to_regex(pattern: str) -> str:
    out, i = [], 0
    while i < len(pattern):
        if pattern[i:i + 3] == "**/":
            out.append("(?:.*/)?"); i += 3
        elif pattern[i:i + 2] == "**":
            out.append(".*"); i += 2
        elif pattern[i] == "*":
            out.append("[^/]*"); i += 1
        elif pattern[i] == "?":
            out.append("[^/]"); i += 1
        elif pattern[i] == "[":
            # Glob character class: [abc], [a-z], [!abc]. Pass through with
            # `!` translated to `^` so it matches glob.glob() semantics.
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(pattern[i])); i += 1
            else:
                cls = pattern[i + 1:j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append("[" + cls + "]"); i = j + 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    return "^" + "".join(out) + "$"


_INCLUDE_RE = [re.compile(_glob_to_regex(p)) for p in INCLUDE]


def matches_include(path: str) -> bool:
    return any(r.match(path) for r in _INCLUDE_RE)


def expand_includes() -> list[str]:
    seen, out = set(), []
    for p in INCLUDE:
        for f in glob.glob(p, recursive=True):
            if os.path.isfile(f) and f not in seen:
                seen.add(f)
                out.append(f)
    return sorted(out)


# --- Markdown parsing ---

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)", re.DOTALL)
_H1_RE = re.compile(r"^# +(.+)$", re.M)
_FIRST_PARA_AFTER_H1_RE = re.compile(r"^# +.+\n+([^\n]+)", re.M)


def parse_md(text: str, fallback_title: str) -> tuple[str, str, str]:
    """Return (title, description, body). Frontmatter overrides H1/paragraph fallbacks."""
    # Normalize line endings so files authored on Windows (CRLF) parse the
    # same as LF — otherwise the frontmatter regex misses the closing ---
    # and the content leaks the YAML.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fm_title = fm_desc = ""
    body = text
    m = _FM_RE.match(text)
    if m:
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip().strip("\"'")
            # Accept `name` as an alias for `title`. First match wins so the
            # author can pick either by ordering them in the frontmatter.
            if k in ("title", "name") and not fm_title:
                fm_title = v
            elif k == "description":
                fm_desc = v
        body = m.group(2)

    h1 = _H1_RE.search(body)
    title = fm_title or (h1.group(1).strip() if h1 else "") or fallback_title

    para = _FIRST_PARA_AFTER_H1_RE.search(body)
    description = fm_desc or (para.group(1).strip() if para else "") or title

    return title, description, body


def build_item(path: str, text: str | None = None) -> dict | None:
    """Build the API payload. `text` lets the delete path inject pre-deletion content."""
    if text is None:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    base = os.path.basename(path)
    if base.endswith(".md"):
        base = base[:-3]
    title, description, body = parse_md(text, fallback_title=base)
    size = len(body.encode("utf-8"))
    if size > MAX_BYTES:
        fail(f"file={path} content exceeds 256 KB (size={size}); split this playbook")
    if size == 0:
        print(f"::warning file={path}::skipping empty content")
        return None
    external_id = path[:-3] if path.endswith(".md") else path
    return {"external_id": external_id, "title": title, "description": description, "content": body}


# --- Git diff for deletes ---

def deleted_paths() -> list[str]:
    if not BEFORE or not AFTER:
        return []
    for sha in (BEFORE, AFTER):
        subprocess.run(["git", "fetch", "--no-tags", "--depth=1", "origin", sha],
                       check=False, stderr=subprocess.DEVNULL)
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-status", "-z", BEFORE, AFTER],
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as e:
        fail(f"git diff failed (exit {e.returncode}); BEFORE={BEFORE} AFTER={AFTER} may be unreachable")
    tokens = [t.decode("utf-8") for t in out.split(b"\x00") if t]
    deleted, i = [], 0
    while i < len(tokens):
        status = tokens[i]
        if status.startswith("R"):
            oldpath = tokens[i + 1]
            i += 3
            if matches_include(oldpath):
                deleted.append(oldpath)
        else:
            path = tokens[i + 1]
            i += 2
            if status[:1] == "D" and matches_include(path):
                deleted.append(path)
    return deleted


def before_content(path: str) -> str | None:
    try:
        out = subprocess.check_output(["git", "show", f"{BEFORE}:{path}"], stderr=subprocess.DEVNULL)
        return out.decode("utf-8")
    except subprocess.CalledProcessError:
        return None


# --- HTTP ---

def post(endpoint: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{API_URL}{endpoint}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        fail(f"{endpoint} failed: HTTP {e.code}\n{body_text}")
    except (urllib.error.URLError, TimeoutError) as e:
        fail(f"{endpoint} failed: {getattr(e, 'reason', e)}")
    return {}  # unreachable


def batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# --- Dry-run rendering ---

def render_dry_run(upserts: list[dict], deletes: list[dict]) -> str:
    def section(label, items):
        lines = [f"### {label} ({len(items)})", ""]
        if not items:
            lines.append("_(none)_")
        else:
            for it in items:
                lines.append(f"- `{it['external_id']}` — {it['title']}")
                lines.append(f"  > {it['description']}")
        return lines

    lines = [
        "## Sundial Context Engine sync — dry run",
        "",
        "_No API calls were made. The following changes would be applied:_",
        "",
        *section("Will add or update", upserts),
        "",
        *section("Will remove", deletes),
        "",
    ]
    return "\n".join(lines)


# --- Main ---

def main() -> None:
    upsert_items = [it for p in expand_includes() if (it := build_item(p))]

    delete_ids: list[str] = []
    delete_items: list[dict] = []
    for path in deleted_paths():
        external_id = path[:-3] if path.endswith(".md") else path
        delete_ids.append(external_id)
        if DRY_RUN:
            # Parse metadata directly — don't go through build_item, which
            # enforces upsert-only constraints (256 KB cap, non-empty
            # content) that don't apply to a delete preview.
            text = before_content(path)
            if text is not None:
                base = os.path.basename(path)
                if base.endswith(".md"):
                    base = base[:-3]
                title, description, _ = parse_md(text, fallback_title=base)
                delete_items.append(
                    {"external_id": external_id, "title": title, "description": description}
                )

    print(f"Plan: {len(upsert_items)} to upsert, {len(delete_ids)} to delete")

    if DRY_RUN:
        print("Dry run: skipping API calls.")
        text = render_dry_run(upsert_items, delete_items)
        sys.stdout.write(text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(text)
        return

    for batch in batched(upsert_items, BATCH):
        resp = post("/api/v1/push/playbooks", {"items": batch})
        for r in resp.get("results", []):
            print(f"  {r['status']}\t{r['external_id']}")
    for batch in batched(delete_ids, BATCH):
        resp = post("/api/v1/push/playbooks/delete", {"external_ids": batch})
        for r in resp.get("results", []):
            print(f"  {r['status']}\t{r['external_id']}")
    print("Done.")


if __name__ == "__main__":
    main()
