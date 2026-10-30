# overleaf-fakegit — User Guide

A fake git remote for old Overleaf instances (like `overleaf.ktu.edu.tr`) that
have no git bridge. This guide walks through every
workflow. The README is the overview; this is the how-to.

---

## 1. The two ways to use it

| Way | Command | What you get |
|-----|---------|--------------|
| **Direct remote** | `git clone overleaf::<project>` | No setup beyond the tool folder. Every `git pull` checks Overleaf itself. |
| **Mirror + server** | `init` + `watch` + `serve` | A local read-only git server (`git://` / `file://`) that friends can clone from. |

Both share the same mirror logic, and both support `git push`.

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

- The first clone replays the **whole history**, one commit per Overleaf
  change (about a second for a small project, ~7 min for ~1200 changes).
- Each `git pull` makes one cheap change-list call; if nothing changed it's
  "Already up to date" in ~1 s; otherwise it replays just the new changes.
- Object transfer is a **native `git upload-pack`** (the `connect` capability):
  real pack negotiation, so only the *new* objects move, delta-compressed —
  not the whole project re-serialized.
- Progress appears as `remote:` lines ("replaying 3 Overleaf version(s) as
  commits…").
- Chat: `fakegit.py chat` inside the clone (see §6).

- Push back: `git push` uploads your changes to Overleaf (see §4b).

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

## 4b. Pushing to Overleaf

```sh
git commit -am "..." && git push            # overleaf:: remote
git push origin main                        # init/mirror workflow
```

- Changed/new files are uploaded in place (docs keep their Overleaf
  history), new folders are created, deleted files/empty folders removed.
- **Rejected (non-fast-forward / "Pull first")** → someone edited on Overleaf
  since your last pull: `git pull`, resolve if needed, push again.
- Only `main`, no force-push, no symlinks/submodules.
- Uploads are limited to 200 files / 15 min per project by the server; a huge
  first push may need to be split.

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

## 6. History, labels and chat

Each commit is one Overleaf change (grouped like Overleaf's History tab),
with the real author, email and timestamp; the body lists added / removed /
renamed files, labels, and `Co-authored-by:` when several people edited.

```sh
git log --format='%h %ad %an: %s' --date=iso
git log --follow -p -- sections/intro.tex     # one file, across renames
git blame -w main.tex                         # who wrote each line
git log -S'200 kbps' -p                       # when a phrase changed
git tag -n                                    # Overleaf labels (overleaf/* tags)
fakegit.py chat [--all] [--json]              # project chat
```

History comes from Overleaf's history API (`apis.md` §2b) — not
rate-limited. Only the `/version/<v>/zip` endpoint is throttled (30/hour/user,
shared with the browser's History tab); it's used only as a last resort.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `your cookie is expired or invalid (the server redirected to /login)` | Cookie expired. Copy a fresh one into `cookie.txt`. |
| `HTTP 429` / `throttled` | Only the version-zip fallback is throttled (30/hour); retry in ~an hour. |
| `no changes on Overleaf since last sync` but you just edited | Overleaf may lag 1–2 min; run `sync --force` if you're sure. |
| `git pull` says up to date but you want the latest state anyway | `git pull` triggers the check itself — if Overleaf genuinely has nothing new, it's up to date. |
| `could not find the project list` | Old instance without the dashboard meta; pass `--project <24-hex-id>` instead of a name. |
| Merge conflicts | Normal git: resolve, `git add`, `git commit`; or `git merge --abort`. |
| `serve` warns nothing is listening | Port already in use — pick `--port N`, or `git daemon` isn't installed. |
| Background `watch`/`serve` die when you log out | Run under `systemd --user` timer or `tmux`/`screen`. |

---

## 9. How it works (30-second version)

1. `git-remote-overleaf` (or `sync`) asks the change list which versions are
   new: `GET /project/<id>/updates` (cheap JSON).
2. Each new change is rebuilt from its parent: file ops from the change list,
   text via `/diff?pathname=…&from=V&to=V`, binaries via `/blob/<hash>` (hash
   from `/changes`), committed onto a bare mirror with pygit2. Labels become
   `overleaf/*` tags.
3. Your clone fetches that mirror through native `git upload-pack`; `git push`
   goes through `git receive-pack`, whose pre-receive hook uploads the change
   via Overleaf's write API before the ref moves.
4. `watch --realtime` keeps a socket.io v1 xhr-polling connection (handshake
   must include `?projectId=`) so edits trigger sync within seconds.
