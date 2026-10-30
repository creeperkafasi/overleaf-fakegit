---
name: overleaf-git
description: Work on the user's Overleaf projects (overleaf.ktu.edu.tr, no official git bridge) through git — list projects, clone, read the full edit history (who changed what, when), read the project chat, proofread/edit locally and push back. Use whenever the user mentions Overleaf or an Overleaf project/paper, or a folder has an `overleaf::` git remote or a fakegit `config.json`. Do NOT use git.overleaf.com or Overleaf git tokens.
---

# Overleaf projects via git (overleaf-fakegit)

The user's Overleaf is the university instance `https://overleaf.ktu.edu.tr`.
It has **no** official git bridge: `git.overleaf.com`, `https://…/git/…` URLs
and Overleaf git tokens do not work. Use the `overleaf::` remote instead.

Installed: `git-remote-overleaf` and `fakegit` are on `PATH` (symlinked in
`~/.local/bin` from `/home/denizt/dev/overleaf-fakegit`). Auth is the session
cookie in `/home/denizt/dev/overleaf-fakegit/cookie.txt`, already set up, so
don't ask for tokens or passwords, and never print or copy the cookie. If a
command says the cookie is expired, stop and ask the user to paste a fresh one
into that file.

## Find and clone a project

```sh
fakegit projects                            # id  name  (access, last update)
fakegit projects --search 6TiSCH
git clone overleaf::<24-hex-id> paper       # or overleaf::"<unique name part>"
```

The first clone replays the **whole Overleaf history** as one commit per
Overleaf change (seconds for small projects, a few minutes for ~1000 changes;
progress is shown as `remote:` lines). Later `git pull`s fetch only new changes.

## Read history (the point of all this)

Every commit is one Overleaf change with the real author, email and timestamp;
the subject is `Overleaf change v<from>→v<to>: <files>`, the body lists
added/removed/renamed files, `Overleaf label: …`, and `Co-authored-by:` for
changes made by several people at once.

```sh
git log --format='%h %ad %an: %s' --date=iso      # timeline
git log -p -- main.tex                            # every edit to a file
git log --follow -p -- figs/x.tex                 # across renames
git blame -w main.tex                             # who wrote each line
git log -S'200 kbps' -p                           # when a phrase appeared/vanished
git log --author=hakanaydin --since=2026-10-01    # one person's edits
git diff overleaf/Submitted-draft-1 HEAD          # since a labelled version
git tag -n                                        # Overleaf labels = overleaf/* tags
```

Changes are grouped like Overleaf's History tab (auto-save bursts), so one
commit can contain several edits by the same session.

## Read the project chat

Collaborators often leave review notes in the chat, not in the text:

```sh
fakegit chat              # last 50 messages, oldest first
fakegit chat --all        # everything
fakegit chat --json       # machine-readable (id, content, timestamp, user{first_name,email})
```

Run it inside the project folder (it finds the project from the `overleaf::`
remote or `config.json`). Comments/track-changes are not available on this
instance.

## Proofread / edit and push

```sh
git pull                                    # always start from the latest state
# ... edit files, compile locally with tectonic to check ...
git commit -am "Fix inconsistent bit rate (200 vs 150 kbps)"
git push                                    # uploads to Overleaf
```

- The push uploads the **final tree** (Overleaf has no commits): changed files
  are replaced in place (their Overleaf history is kept), new files/folders
  created, deleted files removed, `git mv` becomes an Overleaf rename/move.
- Only `main`, fast-forward only. **Rejected / "Pull first"** means someone
  edited on Overleaf since your last pull: `git pull`, resolve conflicts if
  any, push again. Never try to force-push.
- Collaborators may be editing live: keep pushes small and pull right before.
- `git tag -a <name> -m "<label text>" && git push origin <name>` creates an
  Overleaf label — only for a tag on the current `main` tip.
- Don't push generated files (PDF output, `.aux`, …) unless the user asks.

## When not to push

Proofreading requests usually want **suggestions**, not silent edits: unless
the user asked you to apply changes, report findings (file:line, quote,
suggested fix) and let them decide. Edits pushed here appear instantly in
everyone's Overleaf editor.
