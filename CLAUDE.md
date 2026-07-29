# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

The repository contains no application source code yet — only `README.md` (a stub), `LICENSE` (MIT), and a single GitHub Actions workflow. There is no build system, package manifest, dependency lockfile, linter config, or test suite. Do not assume npm/pip/make targets exist; if a task needs one, the toolchain has to be introduced as part of that task.

## Architecture

The only executable logic lives in `.github/workflows/deploy.yml`. It deploys over SSH from a GitHub-hosted runner using `appleboy/ssh-action`, and its notable properties are:

- **Manual trigger only.** `workflow_dispatch` with an `action` input of `inspect` (read-only server probe: OS, listeners on 80/443, nginx status, disk) or `deploy`. There is no push/PR trigger, so merging never deploys — a human dispatches the run.
- **No Docker, deliberately.** nginx is installed via `apt-get` on the target host because pulling images from Docker Hub is unreliable on that server. Don't "modernize" this into a container deploy without an explicit request.
- **The app is currently embedded in the workflow.** The served page is a heredoc (`cat > /var/www/html/index.html <<'HTML'`) inside the deploy step, plus a static `/var/www/html/healthz` file containing `ok`. Editing the placeholder page today means editing YAML. When real application code lands, that heredoc should be replaced by copying files from the repo rather than growing further inline.
- **Serialized runs.** `concurrency: group: deploy-server` with `cancel-in-progress: false`, so deploys queue instead of racing.
- Server state is mutated directly and idempotently (`apt-get install`, rewrite files, `systemctl restart nginx`); there is no build artifact, no versioning, and no rollback path.

## Deploy configuration

Configured through repository secrets (Settings → Secrets and variables → Actions):

| Secret | Required | Default |
| --- | --- | --- |
| `SERVER_HOST` | yes | — |
| `SERVER_PASSWORD` | yes | — |
| `SERVER_USER` | no | `root` |
| `SERVER_PORT` | no | `22` |

Authentication is SSH **password**, not a key. Deploys land on port 80 as `root` by default; `/healthz` returning `ok` is the health contract to preserve.

## Conventions

Comments and user-facing copy in `deploy.yml` and the served page are written in Russian. Match the surrounding language when editing those files.
