# Sundial Context Engine sync

A GitHub Action that keeps your Sundial Context Engine playbook library
in lockstep with markdown files in your repo. What you push to git
is what the agents see.

## Quick start

```yaml
name: Publish Sundial playbooks

on:
  push:
    branches: [main]
  pull_request:

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: sundialso/context-sync-action@v1
        with:
          api_url: https://production.us-east-2.api.sundial.work/context-engine
          token: ${{ secrets.SUNDIAL_CONTEXT_TOKEN }}
          include: |
            playbooks/**/*.md
            guides/**/*.md
            docs/runbooks/**/*.md
```

Create a `sundial_sat_…` token in the Sundial app
(Settings → Service Accounts) with role `data_steward` or above and
store it as the repo secret `SUNDIAL_CONTEXT_TOKEN`.

### Picking the right `api_url`

`api_url` is the Context Engine API base URL, not the web app URL
(`context.sundial.so` won't work — that's the web app and doesn't
serve `POST /api/v1/push/*`).

| Hosting     | `api_url`                                                              |
|-------------|------------------------------------------------------------------------|
| AWS         | `https://production.us-east-2.api.sundial.work/context-engine`         |
| GCP         | `https://sundial-gcp-prod.us-central1.api.sundial.work/context-engine` |
| Self-hosted | Your dedicated API base URL — ask Sundial support.                     |

If unsure which cloud your tenant lives on, check with your Sundial
admin.

## Inputs

| Input      | Required | Default             | Description                                                                       |
|------------|----------|---------------------|-----------------------------------------------------------------------------------|
| `api_url`  | yes      | —                   | Context Engine base URL.                                                          |
| `token`    | yes      | —                   | Service Account Token, stored as a repo secret.                                   |
| `include`  | no       | `playbooks/**/*.md` | Newline-separated glob patterns. Multiple supported.                              |
| `dry_run`  | no       | _auto_              | `true` / `false` to override. Auto = dry-run on `pull_request`, publish on `push`. |

## How it syncs

On every run **every file matching `include` is upserted**. The API
no-ops on unchanged content, so the first run on a repo that already
has playbooks Just Works and re-pushing is cheap.

**Deletes come from the git diff** between the event's before and
after SHAs (`push` → `github.event.before` / `after`;
`pull_request` → PR base / head). Files that were deleted (`D`) or
renamed away from (`R`'s old path) and that match `include` are
removed via the delete API.

`external_id` always equals the file path minus `.md`. Renames are
naturally a delete-old + add-new, which keeps the action stateless —
nothing is committed back to your repo.

> Only `push` and `pull_request` events are supported; the action
> errors out on others.

## Dry run on pull requests

`pull_request` runs are dry-run by default: the action lists every
playbook it would add, update, or remove — with title and description
for each — and makes zero API calls. The summary lands in the
workflow log and on the run summary page (`$GITHUB_STEP_SUMMARY`).

Override with `dry_run: false` to publish from PRs (e.g. to a staging
tenant), or `dry_run: true` to force dry-run on `push`.

## How metadata is derived

| Field         | Source order                                                              |
|---------------|---------------------------------------------------------------------------|
| `external_id` | File path minus `.md`. Always. Not overridable.                           |
| `title`       | Frontmatter `title` (or `name`) → first H1 → filename stem.               |
| `description` | Frontmatter `description` → first non-blank line after the H1 → title.    |
| `content`     | File body. Frontmatter (if any) is stripped before sending.               |

### Optional frontmatter

```markdown
---
title: Top-line metrics
description: ARR / retention / conversion at a glance
---

# Core metrics

The dashboard everyone watches.
```

Only `title` (or `name`) and `description` are read. Simple
`key: value` lines — no nested keys, no multi-line strings.

## Limits and constraints

- **10 playbooks per HTTP request.** The action batches automatically.
- **256 KB per file** (post-frontmatter-strip). Larger files fail the
  run with a clear error — split the playbook.
- **One source per tenant.** A tenant uses either Sundial's built-in
  Git Sync **or** this action for playbooks, not both. The API returns
  `409 SOURCE_CONFLICT` if the other source already owns playbooks.
- **Atomic batches.** Any failure in a batch rolls the whole batch
  back, and the action exits non-zero.

## Local testing

```bash
API_URL=https://production.us-east-2.api.sundial.work/context-engine \
TOKEN=sundial_sat_… \
INCLUDE='playbooks/**/*.md' \
DRY_RUN=true \
GITHUB_EVENT_NAME=push \
EVENT_BEFORE=$(git rev-parse HEAD~1) \
EVENT_AFTER=$(git rev-parse HEAD) \
python3 ./sync.py
```

Single Python file, stdlib only. Depends on `git` + `python3` ≥ 3.10,
both preinstalled on `ubuntu-latest` runners.

## License

MIT — see `LICENSE`.
