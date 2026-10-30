# overleaf-fakegit

A **git remote for Overleaf instances without a git bridge** (like the
university's — `/project/<id>/git` returns 404). `git clone overleaf::<project>`
gives you the project's **full edit history** as real git commits (one per
Overleaf change, real author + timestamp), Overleaf labels as tags, and
`git push` uploads your commits back. The project chat is one command away.
Built so both you and coding agents can use plain git on Overleaf projects
(see `skills/overleaf-git/SKILL.md`).

```
Overleaf history API ──► cache mirror (bare, pygit2) ──► your clone
 (change list, per-version     one commit per change,      git pull / log / blame
  text, blobs, labels)         labels -> overleaf/* tags     git push ──► pre-receive hook
                                                                    ──► Overleaf write API
```

## How history works

The instance runs Overleaf's modern history service, which exposes
(none of these are rate-limited — see `apis.md`):

- `GET /project/<id>/updates` — the change list (versions, authors, file ops)
- `GET /project/<id>/diff?pathname=P&from=V&to=V` — a text file at version V
- `GET /project/<id>/changes?since=N` — the raw change log; file adds carry
  blob hashes, `GET /project/<id>/blob/<hash>` serves the bytes (git SHA-1)
- `GET /project/<id>/labels` — named versions

Each Overleaf change becomes a commit built from its parent tree + that
change's add/remove/rename ops + the new content of the files it touched.
Verified on a 1230-change project: the replayed tip is byte-identical to
Overleaf's own zip, ~7 min for the whole history (4 parallel requests);
incremental pulls take a second or two. The rate-limited
`/version/<v>/zip` (30/hour) is only a last-resort fallback.

## Install (once)

```sh
T=/home/denizt/dev/overleaf-fakegit
ln -s $T/git-remote-overleaf ~/.local/bin/    # git finds overleaf:: remotes
ln -s $T/fakegit.py ~/.local/bin/fakegit      # `fakegit projects`, `fakegit chat`, …
ln -s $T/skills/overleaf-git ~/.claude/skills/ # agents: the overleaf-git skill
```

The scripts resolve symlinks, so the `.venv` and `cookie.txt` in the tool
folder are found from anywhere.

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
   This creates the bare mirror, replays the full history, then
   clones it into your project folder. `--max-updates N` caps the replayed
   history (`--max-updates 0` = snapshot only). The mirror
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

## Direct git remote: `overleaf::<project>`

The bundled `git-remote-overleaf` helper makes an Overleaf project usable as a
real git remote — no mirror setup needed on the client side:

```sh
export PATH="$PATH:/home/denizt/dev/overleaf-fakegit"   # so git finds the helper
git clone overleaf::69b7e11ee0f8759721a6a6f0 my-paper    # by project id
git clone "overleaf::Distance-Aware" my-paper            # or a unique name
cd my-paper
git pull    # checks the change list, downloads only NEW versions, merges
```

- **Full history**: the first fetch replays every Overleaf change as a
  commit; later fetches replay only the new ones. Labels arrive as annotated
  `overleaf/<label>` tags.
- Progress shows as `remote:` lines ("replaying N Overleaf version(s) as
  commits…").
- A local cache mirror is kept at `~/.cache/overleaf-fakegit/<project>/`
  (override with `git config overleaf.cache-dir` / `$OVERLEAF_CACHE_DIR`), so
  re-fetches only download what changed.

## Chat

```sh
fakegit.py chat            # last 50 messages, oldest first
fakegit.py chat --all      # whole chat;  --json for machine-readable output
```

Works in any project folder (`overleaf::` clone or `init` folder). Review
comments / track changes aren't exposed by this instance (404).
- Config: `git config overleaf.url`, `$OVERLEAF_URL`, or the tool folder's
  `config.json`; cookie from `git config overleaf.cookie-file`,
  `$OVERLEAF_COOKIE`, or `cookie.txt` next to the helper.

## Pushing to Overleaf

`git push` works with both the `overleaf::<project>` remote and the
`init` mirror (`git push origin main`):

```sh
vim main.tex && git commit -am "rewrite intro"
git push
# remote: overleaf: uploading 1 file(s), deleting 0…
# remote:   updated main.tex
# remote: overleaf: push applied.
```

The push lands in the mirror via a real `git receive-pack`; its `pre-receive`
hook (`fakegit.py pre-receive`) diffs the Overleaf state against your pushed
tree and, before the ref moves:

- uploads added/changed files (upsert in place — existing docs keep their id
  and Overleaf history; text becomes docs, binaries become files),
- creates missing folders, deletes removed files and folders left empty,
- turns renames/moves (`git mv`, detected like `git diff -M`) into Overleaf
  rename/move — the file keeps its Overleaf history,
- `git push origin <tag>` (an annotated tag on the current `main` tip)
  creates an Overleaf label named after the tag message.

Only the final tree is uploaded (Overleaf has no commits). Safety rails:
fast-forward only (no force-push), `main` only, and the hook re-checks the
change list right before uploading — if a collaborator edited in the meantime
the push is declined with "Pull first". Symlinks/submodules are refused. If an
upload fails midway the ref doesn't move; `git pull`, then push again.

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
- It is strictly read-only: pushes are denied by `git daemon` (push from
  the project folder itself instead).

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

- **Push** (`git push`, both workflows) is main-only and fast-forward-only:
  if Overleaf changed since your last pull the push is rejected — `git pull`,
  then push again. See "Pushing to Overleaf" below.
- **Cookie expiry**: a stale cookie gives a clear error — re-copy from DevTools.
- **Change trigger**: `sync`/`watch`/`git pull` check the cheap change list
  first and only fetch content when the version moved. If the history API is
  unavailable on an instance, it falls back to version zips / snapshots.
- **Rate limiting**: only `/version/<v>/zip` is throttled (30/hour/user,
  `download-project-revision` in the source). History replay doesn't use it
  (except as a last resort for a binary whose hash can't be resolved).

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

- **Empty folders** aren't tracked by git (same as normal git).
- **Timestamps/authors** come from the Overleaf change list (`end_ts`,
  `meta.users[0]`, further users as `Co-authored-by:`); changes are grouped
  like Overleaf's own History tab.

## Layout

```
fakegit.py          the tool (needs pygit2; auto-runs from .venv if present)
cookie.txt          your session cookie (git-ignored)
config.json         default config (git-ignored)
.venv/              pygit2/libgit2 (created by setup, git-ignored)
README.md, guide.md overview / how-to
apis.md             reverse-engineered Overleaf endpoints
skills/overleaf-git/SKILL.md   instructions for coding agents

Per project folder:
  .git/             your working repo
  .overleaf/        mirror.git (bare) + daemon.pid — git-ignored
  config.json       url/project/cookie_file/interval/auto_stash — git-ignored
  .git/fakegit-state.json   last replayed version etc. (inside .git)
```
