# overleaf-fakegit — User Guide

A fake git remote for old Overleaf instances (like `overleaf.ktu.edu.tr`) that
have no git bridge but allow zip downloads. This guide walks through every
workflow. The README is the overview; this is the how-to.

---

## 1. The two ways to use it

| Way | Command | What you get |
|-----|---------|--------------|
| **Direct remote** | `git clone overleaf::<project>` | No setup beyond the tool folder. Every `git pull` checks Overleaf itself. |
| **Mirror + server** | `init` + `watch` + `serve` | A local read-only git server (`git://` / `file://`) that friends can clone from. |

Both share the same cache/mirror logic and the same rate-limit handling.

---

## 2. One-time setup

```sh
# 1. Make sure git finds the remote helper:
export PATH="$PATH:$HOME/dev/overleaf-fakegit"     # add to ~/.zshrc

# 2. Your session cookie (a login token — keep it private):
#    DevTools → Network → reload → any request to overleaf.ktu.edu.tr
#    → Request Headers → Cookie → copy the whole line
echo '<the-cookie-line>' > $HOME/dev/overleaf-fakegit/cookie.txt

# 3. (optional) your instance URL, if it's not the KTU one:
git config --global overleaf.url https://overleaf.ktu.edu.tr
```

That's it. The cookie file and the default URL in `config.json` are picked up
automatically by every command.

---

## 3. Quick start: clone straight from Overleaf

```sh
# list your projects to get ids / names:
python3 $HOME/dev/overleaf-fakegit/fakegit.py projects

git clone overleaf::69b7e11ee0f8759721a6a6f0 my-paper    # by id
git clone "overleaf::Distance-Aware" my-paper            # by unique name
cd my-paper

# work normally:
vim main.tex && git add -A && git commit -m "..."

# pull Overleaf changes (downloads ONLY the new versions):
git pull
```

- First clone takes ~5–20 s: it downloads the **latest snapshot** (one zip,
  never rate-limited) — not the whole history.
- Each `git pull` makes one cheap change-list call; if nothing changed it's
  "Already up to date" in ~2–5 s; if a few versions changed it downloads just
  those and replays them as per-change commits.
- Progress appears as `remote:` lines ("downloading 3 new version(s) from
  overleaf…"). If many versions are pending it says so and serves the latest
  snapshot instead (that's on purpose — see §7).

---

## 4. Full setup: mirror + working repo + read-only server

For a persistent project folder where `sync`/`watch`/`serve` run:

```sh
mkdir -p ~/dev/papers/distance-aware && cd ~/dev/papers/distance-aware
python3 $HOME/dev/overleaf-fakegit/fakegit.py init          # interactive:
#   - instance URL (Enter for default)
#   - project picker (numbered list of your projects)
# or non-interactively:
python3 $HOME/dev/overleaf-fakegit/fakegit.py init --url https://overleaf.ktu.edu.tr \
    --project "Distance-Aware"

# daily:
git add -A && git commit -m "..."                            # local edits
python3 $HOME/dev/overleaf-fakegit/fakegit.py sync           # pull + merge
python3 $HOME/dev/overleaf-fakegit/fakegit.py watch          # keep pulling every 5 min
python3 $HOME/dev/overleaf-fakegit/fakegit.py status         # what's going on
```

`init` creates:
- `.overleaf/mirror.git` — a bare mirror repo (git-ignored)
- your working repo with `origin` = the mirror
- `config.json` (git-ignored) and `.gitignore` entries

`sync` = download new versions → commit them on the mirror → `git fetch origin`
→ merge into your branch (auto-stash, conflict markers — all normal git).

---

## 5. The read-only server (for friends / other machines)

```sh
python3 $HOME/dev/overleaf-fakegit/fakegit.py serve --dir ~/dev/papers/distance-aware --background
```

It verifies the daemon is listening, then prints every address it can be
cloned from (all verified):

```
serving (read-only):
  git://127.0.0.1:9418/mirror.git
  git://100.99.28.29:9418/mirror.git        # tailscale
  git://192.168.1.153:9418/mirror.git       # LAN
  file:///home/denizt/dev/papers/distance-aware/.overleaf/mirror.git
```

- Friends on your LAN: `git clone git://<printed-lan-ip>:9418/mirror.git`
- Bind one interface: `serve --listen 127.0.0.1`; different port: `--port N`
- Stop: `serve --stop`
- **Read-only**: `git daemon` denies pushes.
- Multi-project on one server: run `watch --realtime` per project, and one
  `git daemon --base-path=/srv/overleaf-mirrors --export-all --port=9418`
  where `/srv/overleaf-mirrors/<name>.git` holds the mirrors.

---

## 6. History: when you actually want the per-change commits

By default nothing replays the full history (it's rate-limited, §7). Options:

```sh
# replay the last N changes at init:
python3 .../fakegit.py init --dir . --max-updates 30

# trickle the whole history in the background (~30 commits/hour):
python3 .../fakegit.py watch --dir . --backfill
```

Each commit is one Overleaf change, with the real author and timestamp:

```
hakanaydin <hakanaydin@ktu.edu.tr> 2026-08-16 Overleaf change v462→v463: bare_jrnl_new_sample4.tex
```

`--backfill` merges into your working repo only once the mirror catches up to
the newest version (so the backfill never rewinds your tree).

---

## 7. The rate limit (KTU instance) — why some things are slow

Reverse-engineered from the ShareLaTeX source: the `/project/<id>/version/<v>/zip`
endpoint allows **30 downloads per user per rolling hour**
(`RateLimiterMiddleware.rateLimit({endpointName: 'download-project-revision',
maxRequests: 30, timeInterval: 60*60})`), shared across all your projects and
with the browser's History tab.

Consequences, by design:
- The **latest snapshot** (`/download/zip`) is *not* rate-limited → that's the
  fast default everywhere.
- Replaying history costs one version-zip per commit → at most 30/hour →
  full backfills trickle over hours/days (`--backfill` handles it, resuming
  from the mirror tip every time).
- If a sync is throttled you'll see `throttled: version-download budget
  exhausted (30/hour)…` and it falls back to a snapshot; the mirror resumes
  from where it stopped next time.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `HTTP 302/401/403` or "not a zip file" | Cookie expired. Copy a fresh one into `cookie.txt`. |
| `throttled: version-download budget exhausted` | Used the 30/hour budget; retry in ~an hour, or let `watch`/`--backfill` resume automatically. |
| `no changes on Overleaf since last sync` but you just edited | Overleaf may lag 1–2 min; run `sync --force` if you're sure. |
| `git pull` says up to date but you want the latest state anyway | `git pull` triggers the check itself — if Overleaf genuinely has nothing new, it's up to date. |
| `could not find the project list` | Old instance without the dashboard meta; pass `--project <24-hex-id>` instead of a name. |
| Merge conflicts | Normal git: resolve, `git add`, `git commit`; or `git merge --abort`. |
| `serve` warns nothing is listening | Port already in use — pick `--port N`, or `git daemon` isn't installed. |
| Background `watch`/`serve` die when you log out | Run under `systemd --user` timer or `tmux`/`screen`. |

---

## 9. How it works (30-second version)

1. `git-remote-overleaf` (or `sync`) asks the Overleaf change list which
   versions are new: `GET /project/<id>/updates?min_count=…` (cheap JSON).
2. New versions are downloaded as zips: `GET /project/<id>/version/<v>/zip`
   (rate-limited, §7), extracted, and committed onto a local bare mirror
   (`.overleaf/mirror.git`) with pygit2 — one commit per change, real
   author/timestamp.
3. The working repo fetches/merges that mirror like any git remote; or the
   helper streams the objects to the requesting clone via fast-import.
4. `watch --realtime` keeps a socket.io v1 xhr-polling connection (handshake
   must include `?projectId=` — reverse-engineered from the instance) so edits
   trigger sync within seconds.

The old realtime channel can't serve *historical* content on this instance
(no `loadDocVersions`; its history JSON is stale and non-invertible), so
version zips remain the only per-version content source — hence the rate limit
being the one real constraint.
