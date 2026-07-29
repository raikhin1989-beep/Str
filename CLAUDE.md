# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

The site is a hand-written static page in `site/` — no build system, package manifest, lockfile, linter, or test suite. Do not assume npm/pip/make targets exist; if a task needs one, the toolchain has to be introduced as part of that task, and `site/` then becomes build *output* rather than sources.

## Architecture

`site/` is the deploy payload and `.github/workflows/deploy.yml` is the only executable logic. The workflow deploys over SSH from a GitHub-hosted runner using `appleboy/ssh-action`:

- **Push to `main` deploys.** The `push` trigger is filtered to `site/**` and the workflow file itself, so doc-only commits don't redeploy. `workflow_dispatch` also offers `deploy` and `inspect` (read-only probe: OS, listeners on 80/443, nginx status, live `version`, disk).
- **Whatever is in `site/` becomes the docroot.** The tree is tarred, base64'd into `SITE_B64`, passed through the action's `envs`, and unpacked on the server, then `rsync -a --delete` syncs it into `/var/www/html`. `--delete` means files removed from `site/` disappear from the server — the docroot mirrors the repo, so don't hand-place files on the host expecting them to survive.
- **No Docker, deliberately.** nginx is installed via `apt-get` on the target host because pulling images from Docker Hub is unreliable on that server. Don't "modernize" this into a container deploy without an explicit request.
- **Serialized runs.** `concurrency: group: deploy-server` with `cancel-in-progress: false`, so deploys queue instead of racing.
- Server state is mutated directly and idempotently (`apt-get install`, sync files, `systemctl restart nginx`); there is no build artifact and no rollback path — redeploy an older commit to roll back.

## Verifying a deploy

The workflow writes the deployed commit SHA to `/var/www/html/version`, which is the handle for confirming what is actually live without reading Actions logs:

```
curl http://<SERVER_HOST>/version   # deployed commit SHA
curl http://<SERVER_HOST>/healthz   # -> ok
```

`version` is generated at deploy time, not stored in `site/`. `healthz` is a real file in `site/` and must keep returning `ok` — it's the health contract.

## Deploy configuration

Configured through repository secrets (Settings → Secrets and variables → Actions):

| Secret | Required | Default |
| --- | --- | --- |
| `SERVER_HOST` | yes | — |
| `SERVER_PASSWORD` | yes | — |
| `SERVER_USER` | no | `root` |
| `SERVER_PORT` | no | `22` |

Authentication is SSH **password**, not a key. Deploys land on port 80 as `root` by default.

## Conventions

Comments and user-facing copy in `deploy.yml` and `site/` are written in Russian. Match the surrounding language when editing those files.
