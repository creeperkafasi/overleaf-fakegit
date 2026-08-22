# overleaf-fakegit

A **fake git remote** for zip-only Overleaf instances (like your university's
old one — no git bridge; `/project/<id>/git` returns 404). It turns Overleaf's
own **change list** into a real git history and serves it as a proper
**read-only git server**, so your local workflow is ordinary git.

```
Overleaf change list ──► bare mirror repo (.overleaf/mirror.git) ──► your working repo
(updates API: one           one commit per change,                 (a real clone;
 commit per change,          real author + timestamp)               git merge origin/main)
 real author/timestamp)              │
                                     └─► git daemon (read-only): git:// or file:// clones
```

## Why this design

Probing the instance showed it exposes more than the plain zip:

- `GET /project/<id>/updates?min_count=N` — the full change list (this project:
  1034 changes, v0 → v6352, each with the author's email and a timestamp)
- `GET /project/<id>/version/<v>/zip` — the exact project state at any version

So instead of one snapshot per sync, `init` **replays the whole change list** as
one git commit per Overleaf change — `git log` reads like real history
(`457519 <457519@ogr.ktu.edu.tr> 2026-08-22 10:44:45 Overleaf change v6351→v6352`).
Repos are built with **libgit2 (pygit2)** — no shelling out for object
creation — and served read-only via `git daemon`.

If an instance lacks the updates API, the tool falls back to one snapshot
commit per sync from the latest zip (still through the mirror).

## Setup (one time per project folder)

1. **Find the project** — the tool can list your account's projects for you:
   ```sh
   python3 /home/denizt/dev/overleaf-fakegit/fakegit.py projects
   # 69b7e11ee0f8759721a6a6f0  A Distance-Aware Hybrid Power and ...  (readWrite, updated 2026-08-16)
   python3 /home/denizt/dev/overleaf-fakegit/fakegit.py projects --search 6TiSCH
   python3 /home/denizt/dev/overleaf-fakegit/fakegit.py projects --json
   ```

2. **Session cookie** (a login token — treat it like a password):
   - DevTools → **Network** → reload → click any request to
     `overleaf.ktu.edu.tr` → **Request Headers → Cookie** → copy the whole line
   - Save it next to the script (or in the project folder):
     ```sh
     echo '<the-cookie-line>' > /home/denizt/dev/overleaf-fakegit/cookie.txt
     ```
   - The bare `overleaf.sid` value and the `OVERLEAF_COOKIE` env var also work.

3. **Init** — `init` is interactive: it prompts for the instance URL (default
   `https://overleaf.ktu.edu.tr`), lets you paste a cookie if none is saved,
   and shows a numbered picker of your projects. Everything can also be passed
   as flags for scripting — `--project` accepts either the 24-hex id or a name
   substring:
   ```sh
   mkdir ~/dev/papers/distance-aware-6tisch-mac
   cd ~/dev/papers/distance-aware-6tisch-mac
   python3 /home/denizt/dev/overleaf-fakegit/fakegit.py init                  # interactive
   python3 /home/denizt/dev/overleaf-fakegit/fakegit.py init \
     --url https://overleaf.ktu.edu.tr --project "Distance-Aware"            # by name
   ```
   This creates the bare mirror, replays history (or seeds a snapshot), then
   clones it into your project folder. `--max-updates N` caps the replayed
   history for a quick start (`--max-updates 0` = snapshot only). The mirror
   lives at `.overleaf/mirror.git` (git-ignored); `config.json` is written here
   and also git-ignored.

## Daily use

```sh
# work locally, commit like normal git:
vim main.tex
git add -A && git commit -m "rewrite intro"

# pull Overleaf changes (replay new changes into mirror, then fetch + merge):
python3 /home/denizt/dev/overleaf-fakegit/fakegit.py sync

# ...or keep pulling automatically every 5 minutes:
python3 /home/denizt/dev/overleaf-fakegit/fakegit.py watch            # config interval
python3 /home/denizt/dev/overleaf-fakegit/fakegit.py watch --interval 900

# inspect state:
python3 /home/denizt/dev/overleaf-fakegit/fakegit.py status
```

Because the working repo is a real clone of the mirror, plain git works too:
`git fetch origin && git merge origin/main` — same thing `sync` does.

## Read-only server

```sh
python3 /home/denizt/dev/overleaf-fakegit/fakegit.py serve              # foreground
python3 /home/denizt/dev/overleaf-fakegit/fakegit.py serve --background # daemonize
python3 /home/denizt/dev/overleaf-fakegit/fakegit.py serve --stop       # stop it
```

`serve` verifies the daemon is actually accepting connections, then prints
every address it can be cloned from (all verified working):

```
serving (read-only):
  git://127.0.0.1:9418/mirror.git
  git://100.99.28.29:9418/mirror.git        # tailscale
  git://192.168.1.153:9418/mirror.git       # LAN
  file:///home/denizt/dev/papers/868-mhz-review/.overleaf/mirror.git   (no server needed)
```

- Bind a specific interface: `serve --listen 127.0.0.1` (default: all).
- Other machines on your LAN: `git clone git://<printed-lan-ip>:9418/mirror.git`
- No server at all: `git clone file:///path/to/project/.overleaf/mirror.git`
- It is strictly read-only: pushes are denied by `git daemon`.

## How merging behaves

- **No local commits** since the last sync → merge fast-forwards (well, `--no-ff`
  makes a tidy merge commit), tree becomes the Overleaf state.
- **Local commits + remote changes in different files** → clean 3-way merge.
- **Same lines edited both sides** → conflict markers; fix, `git add`, `git commit`;
  bail with `git merge --abort`.
- **Uncommitted local edits when syncing** → auto-stashed before the merge and
  restored after (`git stash pop`); set `"auto_stash": false` in `config.json`
  to refuse instead.

## Background sync (systemd user timer)

```ini
# ~/.config/systemd/user/overleaf-sync.service
[Unit]
Description=Overleaf sync

[Service]
Type=oneshot
WorkingDirectory=/home/denizt/dev/papers/868-mhz-review
ExecStart=/usr/bin/python3 /home/denizt/dev/overleaf-fakegit/fakegit.py sync

# ~/.config/systemd/user/overleaf-sync.timer
[Timer]
OnBootSec=1min
OnUnitActiveSec=10min
```

```sh
systemctl --user daemon-reload && systemctl --user enable --now overleaf-sync.timer
```

(Or a cron line: `*/10 * * * * python3 /home/denizt/dev/overleaf-fakegit/fakegit.py sync --dir /home/denizt/dev/papers/868-mhz-review`.)

## Notes & limitations

- **One-way**: Overleaf → local only. Push back via the web editor (or the
  two-way `overleaf-sync` CLI at `/home/denizt/dev/overleaf-sync`).
- **Cookie expiry**: a stale cookie gives a clear error — re-copy from DevTools.
- **Change trigger**: `sync`/`watch` check the cheap updates API first and only
  download zips when the version moved. If the API is unavailable on an
  instance, it falls back to full-zip snapshots.
- **Rate limiting (KTU instance)**: reverse-engineered from the ShareLaTeX web
  source (`research/src-web/app/src/router.js`): the `/version/<v>/zip` endpoint
  is limited to **30 downloads per user per rolling hour**
  (`RateLimiterMiddleware.rateLimit({endpointName: 'download-project-revision',
  maxRequests: 30, timeInterval: 60*60})`). The tool downloads version zips
  serially, commits each version as it arrives, and resumes from the mirror tip,
  so a throttled run never re-downloads finished versions. Practical path:
  - `init` seeds from the latest snapshot (not throttled) and records the
    current version, so **daily syncs only pull new changes** — the mirror
    accumulates real per-change history from now on;
  - to backfill older history, re-run `init --max-updates N` (~30 per hour
    budget; an interrupted run resumes from the mirror tip), then `sync`.

## Realtime updates (`watch --realtime`)

The old realtime service (Socket.IO v1) is reverse-engineered and works from a
script: the **handshake must include `?projectId=`**, which binds the session
to the project room — the first poll then delivers `joinProjectResponse` (the
current project state incl. every file hash), and subsequent polls long-poll
for events; collaborators' edits arrive as `otUpdateApplied` frames
(xhr-polling transport — the websocket upgrade 502s behind this instance's
nginx, so the tool uses xhr-polling via stdlib).

```sh
python3 /home/denizt/dev/overleaf-fakegit/fakegit.py watch --realtime
```

This keeps a live connection and runs `sync` within seconds of any edit (with
an interval safety net). Perfect for a server that mirrors a shared project.

## Hetzner / multi-user deployment

The tool is per-project: one folder per project, each with its own
`config.json` + `cookie.txt` (one Overleaf account per friend), its own
`.overleaf/mirror.git`, and its own `watch --realtime` (or systemd timer).
`serve` runs `git daemon` read-only on a project's mirror; to serve several
mirrors at once, give `git daemon` a base path containing them:

```sh
git daemon --reuseaddr --base-path=/srv/overleaf-mirrors --export-all --port=9418
# /srv/overleaf-mirrors/<friend>.git  ->  git clone git://<host>:9418/<friend>.git
```

Each friend's backfill counts against their own Overleaf account's rate limit
(30 version-zips/hour) — with `--max-updates` pacing that's fine.
- **Empty folders** aren't tracked by git (same as normal git).
- **Per-version content is zips-only on this instance**: the realtime channel
  serves the current project state and edit events but has no `loadDocVersions`
  (this instance uses the newer v1-history architecture), `/updates` carries no
  text ops, and `/latest/history` is stale (its ops are non-invertible anyway —
  they contain deletes). So history replay needs the rate-limited version zips;
  the realtime channel is used for **edit notifications** (`watch --realtime`).
- **Timestamps/authors** come from the Overleaf change list (`end_ts`,
  `meta.users[0]`); auto-saves produce one commit each, like Overleaf's own
  History tab.

## Layout

```
fakegit.py          the tool (needs pygit2; auto-runs from .venv if present)
cookie.txt          your session cookie (git-ignored)
config.json         default config (git-ignored)
.venv/              pygit2/libgit2 (created by setup, git-ignored)
README.md

Per project folder:
  .git/             your working repo
  .overleaf/        mirror.git (bare) + daemon.pid — git-ignored
  config.json       url/project/cookie_file/interval/auto_stash — git-ignored
  .git/fakegit-state.json   last replayed version etc. (inside .git)
```
