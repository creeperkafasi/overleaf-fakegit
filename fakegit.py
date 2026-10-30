#!/usr/bin/env python3
"""
overleaf-fakegit — a fake git remote for zip-only Overleaf instances.

Your university Overleaf is old: no git bridge (the /project/<id>/git endpoint
404s), but it DOES serve the project as a zip and — as it turns out — exposes
the full change list and versioned zips:

    GET /project/<id>/updates?min_count=N            -> JSON change list
    GET /project/<id>/version/<v>/zip                -> exact project state at v

This tool exploits that to build a *real* git history out of Overleaf's own
change list, and serves it as a proper read-only git remote:

  - A BARE MIRROR repo (<project-dir>/.overleaf/mirror.git, created with
    libgit2 via pygit2) replays the Overleaf change list as one git commit per
    update — with the real author (email) and timestamp from each change.
  - Your WORKING repo is a normal clone of that mirror (git remote 'origin').
    You edit and commit locally; `sync` replays new Overleaf changes into the
    mirror, then fetches and merges origin/main into your branch with normal
    git semantics (fast-forward, 3-way merge, conflict markers, auto-stash).
  - `serve` runs git daemon on the mirror, so any machine/tool can pull from it
    read-only (git:// or file://) — a genuine read-only server for the fake repo.

If the updates API is unavailable on some instance, the tool falls back to one
snapshot commit per sync from the latest zip (still via the mirror).

Commands:
  init    create mirror + working repo, replay full Overleaf history
  sync    replay new changes into the mirror, then fetch + merge into your branch
  watch   sync periodically (checks the cheap updates API first)
  serve   run a read-only git daemon on the mirror (git:// + file://)
  status  show config / mirror / last sync / history

`git push origin main` uploads your commits to Overleaf: the mirror's
pre-receive hook diffs the pushed tree against the Overleaf state and
uploads / deletes files (fast-forward only; rejected if Overleaf changed
since your last sync).

Auth: your `overleaf.sid` session cookie — via --cookie, OVERLEAF_COOKIE,
cookie.txt in the project dir, or cookie.txt next to this script. Accepts the
full Cookie header, the URL-encoded sid, or the bare sid.

Config lives in <project-dir>/config.json:
  {
    "url":         "https://overleaf.ktu.edu.tr",
    "project":     "6993449de0f8759721a4daa7",
    "cookie_file": "cookie.txt",
    "interval":    300,          // watch default, seconds
    "auto_stash":  true          // stash local changes before merging
  }

Requires: Python 3, pygit2 (libgit2) — auto-runs from the bundled .venv if
present — and the git CLI (>= 2.28) for working-tree operations.
"""

import argparse
import fcntl
import hashlib
import html
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# --------------------------------------------------------------------------- pygit2 (with .venv auto re-exec)

_VENV_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
try:
    import pygit2
except ImportError:
    if os.path.exists(_VENV_PY) and sys.executable != _VENV_PY:
        # -u keeps stdout unbuffered after the re-exec (the original -u is an
        # interpreter flag, not part of sys.argv)
        os.execv(_VENV_PY, [_VENV_PY, "-u"] + sys.argv)
    print("error: pygit2 is required. Install it once:\n"
          f"  python3 -m venv {os.path.dirname(os.path.dirname(_VENV_PY))} "
          f"&& {_VENV_PY} -m pip install pygit2\n"
          "then run this script again (it will use the venv automatically).",
          file=sys.stderr)
    sys.exit(1)

APP_NAME = "overleaf-fakegit"
STATE_FILE = ".git/fakegit-state.json"       # working repo state
LOCK_FILE = ".git/fakegit.lock"
STAGE_DIR = "stage"
OVERLEAF_DIR = ".overleaf"                    # mirror + daemon live here (gitignored)
MIRROR_DIR = os.path.join(OVERLEAF_DIR, "mirror.git")
DAEMON_PID = os.path.join(OVERLEAF_DIR, "daemon.pid")
ZIP_MAGIC = b"PK\x03\x04"
DEFAULT_COOKIE_FILE = "cookie.txt"
CONFIG_NAME = "config.json"
MAIN_REF = "refs/heads/main"
MSG_RE = re.compile(r"Overleaf change v(\d+)→v(\d+)")

# --------------------------------------------------------------------------- errors

class FgError(Exception):
    """Expected, user-facing failure (cookie expired, merge conflict, ...)."""

class GitError(FgError):
    pass

class SyncError(FgError):
    pass

class AuthError(SyncError):
    """Cookie expired/invalid — never worth retrying or falling back."""

COOKIE_EXPIRED_MSG = (
    "your cookie is expired or invalid (the server redirected to /login). "
    "Copy a fresh one from the browser (F12 -> Network -> request headers -> "
    "Cookie) into the cookie file, then retry.")

# --------------------------------------------------------------------------- git CLI helper (working repo ops)

def git(repo_dir, *args, check=True, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(["git", "-C", repo_dir, *args],
                       capture_output=True, text=True, env=e)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed:\n{p.stderr.strip()}")
    return p

# --------------------------------------------------------------------------- config / cookie

DEFAULTS = {
    "url": None,
    "project": None,
    "cookie_file": DEFAULT_COOKIE_FILE,
    "interval": 300,
    "auto_stash": True,
    "zip_delay": 0.3,          # seconds between version-zip downloads
}

def config_path(project_dir):
    return os.path.join(project_dir, CONFIG_NAME)

def load_config(project_dir):
    cfg = {}
    p = config_path(project_dir)
    if os.path.exists(p):
        try:
            with open(p) as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise FgError(f"cannot read {p}: {e}")
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg

def save_config(project_dir, cfg):
    with open(config_path(project_dir), "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")

def normalize_cookie(raw):
    """Accept three paste styles: full Cookie header, encoded sid, bare sid."""
    raw = raw.strip()
    if not raw:
        raise FgError("cookie is empty")
    if "=" in raw:
        return raw
    if raw.startswith("s%3A") or raw.startswith("s:"):
        return "overleaf.sid=" + raw
    return "overleaf.sid=s:" + raw

def resolve_cookie(project_dir, cfg, cli_cookie=None, cli_cookie_file=None):
    raw = None
    if cli_cookie:
        raw = cli_cookie
    elif os.environ.get("OVERLEAF_COOKIE"):
        raw = os.environ["OVERLEAF_COOKIE"]
    else:
        candidates = []
        if cli_cookie_file:
            candidates.append(cli_cookie_file)
        cfile = cfg.get("cookie_file") or DEFAULT_COOKIE_FILE
        candidates.append(os.path.join(project_dir, cfile))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       DEFAULT_COOKIE_FILE))
        for c in candidates:
            if os.path.exists(c):
                try:
                    with open(c) as f:
                        raw = f.read()
                except OSError as e:
                    raise FgError(f"cannot read cookie file {c}: {e}")
                break
    if not raw:
        raise FgError(
            "no cookie found. Provide --cookie <value>, the OVERLEAF_COOKIE "
            "environment variable, or a cookie file (default: cookie.txt in the "
            "project dir or next to this script). See README.")
    return normalize_cookie(raw)

# --------------------------------------------------------------------------- http

def http_get(cfg, cookie, path, timeout=60, retries=3):
    base = (cfg["url"] or "").rstrip("/")
    pid = cfg.get("project")
    if not base:
        raise FgError("config needs url (run `init` with --url)")
    url = f"{base}{path}"
    headers = {
        "Cookie": cookie,
        "User-Agent": f"{APP_NAME}/1.0",
    }
    if pid:
        headers["Referer"] = f"{base}/project/{pid}"
    req = urllib.request.Request(url, headers=headers)
    last_err = None
    for attempt in range(max(1, retries)):   # retry transient network errors
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # an expired session 302s to /login, which urllib follows
                # silently — catch it here instead of failing on the HTML later
                if urllib.parse.urlsplit(resp.geturl()).path.rstrip("/") == "/login":
                    raise AuthError(COOKIE_EXPIRED_MSG)
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f"HTTP {e.code} — {COOKIE_EXPIRED_MSG}")
            raise SyncError(f"HTTP {e.code} for {path}")
        except urllib.error.URLError as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2)
                continue
    raise SyncError(f"network error: {last_err.reason if last_err else 'unknown'}")

def _zip_or_die(data, what):
    if not data.startswith(ZIP_MAGIC):
        raise SyncError(f"{what}: the server did not return a zip file (got an "
                        f"HTML page — probably a redirect to login, i.e. an "
                        f"expired/invalid cookie).")
    return data

def download_latest_zip(cfg, cookie):
    return _zip_or_die(http_get(cfg, cookie, f"/project/{cfg['project']}/download/zip"),
                       "download")

def download_version_zip(cfg, cookie, version):
    return _zip_or_die(http_get(cfg, cookie, f"/project/{cfg['project']}/version/{version}/zip"),
                       f"version {version} zip")

def fetch_updates(cfg, cookie, min_count=500, before=None):
    """Return the newest-first update list (and nextBeforeTimestamp) from the
    Overleaf change-list API."""
    path = f"/project/{cfg['project']}/updates?min_count={min_count}"
    if before:
        path += f"&before={before}"
    data = http_get(cfg, cookie, path, timeout=10, retries=2)
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise SyncError(f"updates API returned unparseable JSON: {e}")
    if not isinstance(obj, dict) or "updates" not in obj:
        raise SyncError("updates API response missing 'updates' list "
                        "(instance may not support the change list)")
    return obj.get("updates", []), obj.get("nextBeforeTimestamp")

def _is_updates_error(e):
    s = str(e)
    return "/updates" in s or s.startswith("updates API")

def fetch_all_updates(cfg, cookie, after_toV=None, max_updates=None):
    """All updates needed: newest-first. Stops once we've passed after_toV or
    hit max_updates. Returns list ordered oldest->newest."""
    updates = []
    before = None
    for _ in range(200):                       # safety cap on pages
        page, nbt = fetch_updates(cfg, cookie, min_count=200, before=before)
        updates.extend(page)
        if max_updates is not None and len(updates) >= max_updates:
            break
        if not page:
            break
        oldest = page[-1]
        if after_toV is not None and oldest["toV"] <= after_toV:
            break
        if not nbt or nbt == before:
            break
        before = nbt
    if max_updates is not None:
        updates = updates[:max_updates]
    if after_toV is not None:
        updates = [u for u in updates if u["toV"] > after_toV]
    updates.reverse()                          # oldest -> newest
    return updates

# --------------------------------------------------------------------------- zip / hash

def extract_zip(data, dest):
    """Extract zip bytes to dest (wiped first), collapsing a single top-level
    folder, with zip-slip protection."""
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise SyncError(f"not a valid zip: {e}")
    with zf:
        for info in zf.infolist():
            name = info.filename
            norm = os.path.normpath(name)
            if norm.startswith(("..", "/")) or os.path.isabs(norm):
                raise SyncError(f"zip contains unsafe path: {name!r}")
            target = os.path.join(dest, norm)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    entries = os.listdir(dest)
    if len(entries) == 1:
        only = os.path.join(dest, entries[0])
        if os.path.isdir(only):
            tmp = dest + ".tmp"
            os.rename(only, tmp)
            os.rmdir(dest)
            os.rename(tmp, dest)

def content_hash(root):
    h = hashlib.sha256()
    rels = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            rels.append(os.path.relpath(os.path.join(dirpath, fn), root))
    for rel in rels:
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        with open(os.path.join(root, rel), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()

# --------------------------------------------------------------------------- mirror (pygit2)

def mirror_path(project_dir):
    return os.path.join(project_dir, MIRROR_DIR)

def ensure_mirror(project_dir):
    path = mirror_path(project_dir)
    if not os.path.isdir(os.path.join(path, "objects")):
        raise FgError("no mirror repo yet — run `init` first.")
    return path

def open_mirror(project_dir):
    return pygit2.Repository(ensure_mirror(project_dir))

def create_mirror(project_dir):
    path = mirror_path(project_dir)
    if os.path.isdir(os.path.join(path, "objects")):
        return path
    os.makedirs(path, exist_ok=True)
    repo = pygit2.init_repository(path, bare=True, initial_head="main")
    return path

def mirror_tip_toV(repo):
    """Version replayed so far, parsed from the tip commit message (None if
    the tip is a snapshot/fallback commit or the mirror is empty)."""
    if repo.head_is_unborn:
        return None
    tip = repo[repo.head.target]
    m = MSG_RE.search(tip.message or "")
    return int(m.group(2)) if m else None

def build_tree_from_dir(repo, root):
    """Build a git tree object from a directory tree (in-process, libgit2)."""
    def build(rel):
        builder = repo.TreeBuilder()
        base = os.path.join(root, rel) if rel else root
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.islink(path):
                with open(path, "rb") as f:
                    blob = repo.create_blob(f.read())
                builder.insert(name, blob, pygit2.GIT_FILEMODE_LINK)
            elif os.path.isdir(path):
                builder.insert(name, build(os.path.join(rel, name) if rel else name),
                               pygit2.GIT_FILEMODE_TREE)
            else:
                blob = repo.create_blob_fromdisk(path)
                mode = (pygit2.GIT_FILEMODE_EXECUTABLE
                        if os.access(path, os.X_OK) else pygit2.GIT_FILEMODE_BLOB)
                builder.insert(name, blob, mode)
        return builder.write()
    return build("")

def signature_from_meta(meta):
    users = meta.get("users") or []
    if users:
        u = users[0]
        name = ((u.get("first_name") or "") + " " + (u.get("last_name") or "")).strip()
        name = name or u.get("email") or "Overleaf user"
        email = u.get("email") or "overleaf@localhost"
    else:
        name, email = "Overleaf", "overleaf@localhost"
    ts_ms = meta.get("end_ts") or meta.get("start_ts")
    ts = int(ts_ms / 1000) if ts_ms else int(time.time())
    offset = time.localtime(ts).tm_gmtoff // 60
    return pygit2.Signature(name, email, time=ts, offset=offset)

def _user_ident(u):
    name = ((u.get("first_name") or "") + " " + (u.get("last_name") or "")).strip()
    return f"{name or u.get('email') or 'Overleaf user'} <{u.get('email') or 'overleaf@localhost'}>"

def commit_message(u):
    """Subject keeps the `Overleaf change vX→vY` form (MSG_RE relies on it);
    file ops, labels and extra authors go in the body."""
    paths = list(u.get("pathnames") or [])
    body = []
    for op in u.get("project_ops") or []:
        if "add" in op:
            body.append(f"added {op['add']['pathname']}")
            paths.append(op["add"]["pathname"])
        elif "remove" in op:
            body.append(f"removed {op['remove']['pathname']}")
            paths.append(op["remove"]["pathname"])
        elif "rename" in op:
            r = op["rename"]
            body.append(f"renamed {r['pathname']} -> {r['newPathname']}")
            paths.append(r["newPathname"])
    paths = list(dict.fromkeys(paths))
    p = ", ".join(paths[:5])
    if len(paths) > 5:
        p += f" (+{len(paths) - 5} more)"
    for l in u.get("labels") or []:
        if l.get("comment"):
            body.append(f"Overleaf label: {l['comment']}")
    trailers = [f"Co-authored-by: {_user_ident(x)}"
                for x in ((u.get("meta") or {}).get("users") or [])[1:]]
    msg = f"Overleaf change v{u['fromV']}→v{u['toV']}: {p}"
    if body:
        msg += "\n\n" + "\n".join(body)
    if trailers:
        msg += "\n\n" + "\n".join(trailers)
    return msg

def commit_with_sig(repo, tree, message, sig):
    """Commit a tree onto the mirror's main branch (root commit if unborn)."""
    parents = [repo.head.target] if not repo.head_is_unborn else []
    return repo.create_commit(MAIN_REF, sig, sig, message, tree, parents)

def commit_snapshot(repo, tree, message):
    sig = pygit2.Signature("overleaf-fakegit", "fakegit@localhost",
                           time=int(time.time()),
                           offset=time.localtime().tm_gmtoff // 60)
    return commit_with_sig(repo, tree, message, sig)

def replay_update(repo, stage_dir, update):
    """Build a commit for one Overleaf change onto the mirror main branch,
    using the change's real author and timestamp."""
    tree = build_tree_from_dir(repo, stage_dir)
    sig = signature_from_meta(update.get("meta") or {})
    return commit_with_sig(repo, tree, commit_message(update), sig)

def apply_version_zip(repo, project_dir, data, update):
    """Extract a version zip into a temp dir and commit it onto the mirror."""
    stage = os.path.join(project_dir, STAGE_DIR)
    try:
        extract_zip(data, stage)
        return replay_update(repo, stage, update)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

# --------------------------------------------------------------------------- replay / snapshot

def _retryable(msg):
    return ("429" in msg or "HTTP 50" in msg or msg.startswith("network error"))

class _ReplayPacer:
    """Adaptive pacing for version-zip downloads. The instance throttles the
    /version/<v>/zip endpoint (30/hour/user). In trickle mode (backfill) we
    give up quickly per tick and let the next sync resume, so watch can slowly
    grind through a large history within the hourly budget."""
    def __init__(self, cfg, trickle=False):
        self.delay = float(cfg.get("zip_delay") or 1.0)
        self.consecutive = 0
        self.trickle = trickle

    def fetch(self, fetch_zip, version, attempts=3):
        for i in range(attempts):
            try:
                data = fetch_zip(version)
            except SyncError as e:
                if not _retryable(str(e)):
                    raise
                self.consecutive += 1
                # give up fast: the budget is per rolling hour, so a 429 (or
                # repeated errors) won't clear by waiting within this run
                if "HTTP 429" in str(e) or self.consecutive >= 2:
                    raise SyncError(
                        "throttled: version-download budget exhausted "
                        "(30/hour) — the mirror resumes from here on the "
                        "next sync." if self.trickle else
                        "throttled: version-download budget exhausted "
                        "(30/hour) — retry in about an hour; the mirror "
                        "resumes from where it stopped.")
                wait = 15.0 if self.trickle else 10.0
                print(f"  transient error on v{version} ({e}) — "
                      f"retrying in {wait:.0f}s …")
                time.sleep(wait)
                continue
            self.consecutive = 0
            self.delay = max(0.2, self.delay / 2)   # recover slowly
            return data
        raise SyncError(f"giving up on v{version} after {attempts} attempts")

# --------------------------------------------------------------------------- history API engine (no rate limit)
#
# Per-version content without the throttled version zips:
#   text   GET /project/<id>/diff?pathname=P&from=V&to=V   -> [{"u": <full text>}]
#   binary GET /project/<id>/blob/<hash>                    (hash = git blob sha1)
# Binary hashes come from the raw change log (GET /project/<id>/changes?since=N:
# one entry per version; file adds carry the blob hash). A version zip is
# only a last resort.

BLOB_MODE = pygit2.GIT_FILEMODE_BLOB

class HistoryAPI:
    def __init__(self, cfg, cookie):
        self.cfg, self.cookie = cfg, cookie
        self.P = f"/project/{cfg['project']}"
        self._changes = None
        self._zips = {}

    def _json(self, path):
        data = http_get(self.cfg, self.cookie, self.P + path, timeout=120)
        try:
            return json.loads(data)
        except ValueError as e:
            raise SyncError(f"history API: bad JSON from {path}: {e}")

    def files_at(self, v):
        """[(path, editable)] of the project at version v."""
        return [(e["pathname"], e.get("editable", True))
                for e in self._json(f"/filetree/diff?from={v}&to={v}")["diff"]
                if e.get("operation") != "removed"]

    def content(self, path, v):
        """Bytes of `path` at version v (text via the diff API, binary via
        its blob)."""
        q = urllib.parse.quote(path, safe="")
        d = self._json(f"/diff?pathname={q}&from={v}&to={v}")["diff"]
        if isinstance(d, dict) and d.get("binary"):
            return self.binary(path, v)
        return "".join(s.get("u", "") + s.get("i", "") for s in d).encode("utf-8")

    def _load_changes(self):
        """Every file's blob hash per version, from the raw change log
        (/changes: one entry per version; adds carry the blob hash)."""
        events, cur, since = {}, {}, 0
        while True:
            page = self._json(f"/changes?since={since}&paginated=true")
            changes = page["changes"] if isinstance(page, dict) else page
            for i, c in enumerate(changes):
                v = since + i + 1                  # change k turns v(k) into v(k+1)
                for o in c.get("operations") or []:
                    p = o.get("pathname")
                    if "file" in o:
                        cur[p] = o["file"].get("hash")
                        events.setdefault(p, []).append((v, cur[p]))
                    elif "newPathname" in o:
                        hsh = cur.pop(p, None)
                        events.setdefault(p, []).append((v, None))
                        if o["newPathname"]:
                            cur[o["newPathname"]] = hsh
                            events.setdefault(o["newPathname"], []).append((v, hsh))
            since += len(changes)
            if not (isinstance(page, dict) and page.get("hasMore")) or not changes:
                break
        self._changes = events

    def _hash_at(self, path, v):
        if self._changes is None:
            self._load_changes()
        hsh = None
        for ev, eh in self._changes.get(path, []):
            if ev <= v:
                hsh = eh
        return hsh

    def binary(self, path, v):
        hsh = self._hash_at(path, v)
        if hsh:
            data = http_get(self.cfg, self.cookie, f"{self.P}/blob/{hsh}", timeout=300)
            if pygit2.hash(data) == pygit2.Oid(hex=hsh):
                return data
        # last resort: the (rate-limited) version zip
        if v not in self._zips:
            self._zips = {v: download_version_zip(self.cfg, self.cookie, v)}
        with zipfile.ZipFile(io.BytesIO(self._zips[v])) as zf:
            try:
                return zf.read(path)
            except KeyError:
                raise SyncError(f"{path} missing from the v{v} zip")

def flat_tree(repo, tree, prefix=""):
    """{path: (oid, mode)} of a git tree."""
    out = {}
    for e in tree:
        p = prefix + e.name
        if e.filemode == pygit2.GIT_FILEMODE_TREE:
            out.update(flat_tree(repo, repo[e.id], p + "/"))
        else:
            out[p] = (e.id, e.filemode)
    return out

def tree_from_flat(repo, flat):
    nested = {}
    for path, entry in flat.items():
        *dirs, name = path.split("/")
        d = nested
        for x in dirs:
            d = d.setdefault(x, {})
        d[name] = entry
    def build(d):
        b = repo.TreeBuilder()
        for name in sorted(d):
            v = d[name]
            if isinstance(v, dict):
                b.insert(name, build(v), pygit2.GIT_FILEMODE_TREE)
            else:
                b.insert(name, v[0], v[1])
        return b.write()
    return build(nested)

def _update_fetches(u):
    """Paths whose content at u['toV'] is needed to build this version."""
    need = set(u.get("pathnames") or [])
    for op in sorted(u.get("project_ops") or [], key=lambda o: o.get("atV", 0)):
        if "add" in op:
            need.add(op["add"]["pathname"])
        elif "remove" in op:
            need.discard(op["remove"]["pathname"])
        elif "rename" in op:
            r = op["rename"]
            if r["pathname"] in need:
                need.discard(r["pathname"])
                need.add(r["newPathname"])
    return need

def replay_history_api(project_dir, cfg, cookie, updates, workers=4, log=print):
    """Replay updates (oldest->newest, contiguous) into the mirror with the
    history API: each commit = parent tree + that update's file ops + the
    new content of the files it touched. Returns #new commits."""
    import concurrent.futures as cf
    repo = open_mirror(project_dir)
    tip_toV = mirror_tip_toV(repo)
    todo = [u for u in updates if tip_toV is None or u["toV"] > tip_toV]
    if not todo:
        return 0
    api = HistoryAPI(cfg, cookie)
    if repo.head_is_unborn:
        flat = {}
        base_v = todo[0]["fromV"]
        if base_v > 0:              # partial history: root = state at base_v
            files = [p for p, _ in api.files_at(base_v)]
            with cf.ThreadPoolExecutor(workers) as ex:
                for p, data in zip(files, ex.map(lambda p: api.content(p, base_v), files)):
                    flat[p] = (repo.create_blob(data), BLOB_MODE)
            sig = pygit2.Signature("overleaf-fakegit", "fakegit@localhost")
            commit_with_sig(repo, tree_from_flat(repo, flat),
                            f"Overleaf change v0→v{base_v}: (earlier history "
                            f"squashed)", sig)
            log(f"  base: state at v{base_v} ({len(flat)} files)")
    else:
        flat = flat_tree(repo, repo[repo.head.target].tree)

    def fetch(u):
        return {p: api.content(p, u["toV"]) for p in _update_fetches(u)}

    n = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        for i in range(0, len(todo), 32):               # bounded lookahead
            batch = todo[i:i + 32]
            for u, contents in zip(batch, ex.map(fetch, batch)):
                for op in sorted(u.get("project_ops") or [], key=lambda o: o.get("atV", 0)):
                    if "remove" in op:
                        flat.pop(op["remove"]["pathname"], None)
                    elif "rename" in op:
                        r = op["rename"]
                        if r["pathname"] in flat:
                            flat[r["newPathname"]] = flat.pop(r["pathname"])
                for p, data in contents.items():
                    flat[p] = (repo.create_blob(data), BLOB_MODE)
                tree = tree_from_flat(repo, flat)
                commit_with_sig(repo, tree, commit_message(u),
                                signature_from_meta(u.get("meta") or {}))
                n += 1
            log(f"  replayed {n}/{len(todo)} (v{batch[-1]['toV']})")
    return n

# --------------------------------------------------------------------------- labels <-> tags

LABEL_TAG_PREFIX = "overleaf/"

def label_tag_name(comment):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", comment or "").strip("-.") or "label"
    return LABEL_TAG_PREFIX + slug[:60]

def sync_labels(project_dir, cfg, cookie, log=print):
    """Mirror Overleaf labels (named versions) as annotated tags
    overleaf/<comment> on the commit of that version. Returns #new tags."""
    repo = open_mirror(project_dir)
    if repo.head_is_unborn:
        return 0
    try:
        labels = json.loads(http_get(cfg, cookie, f"/project/{cfg['project']}/labels"))
    except (SyncError, ValueError):
        return 0
    if not labels:
        return 0
    versions = []                               # (toV, commit), newest first
    for c in repo.walk(repo.head.target, pygit2.GIT_SORT_TOPOLOGICAL):
        m = MSG_RE.search(c.message or "")
        if m:
            versions.append((int(m.group(2)), c.id))
    have = set()                                # subjects of existing tags
    for ref in repo.references:
        if ref.startswith("refs/tags/"):
            obj = repo.revparse_single(ref)
            if obj.type == pygit2.GIT_OBJECT_TAG:
                have.add((obj.message or "").partition("\n")[0].strip())
    n = 0
    for l in labels:
        name = label_tag_name(l.get("comment"))
        if f"refs/tags/{name}" in repo.references or l.get("comment") in have:
            continue
        target = next((cid for v, cid in versions if v <= l.get("version", -1)), None)
        if target is None:
            continue                            # label predates the mirror
        sig = signature_from_meta({"users": [l.get("user") or {}],
                                   "end_ts": _iso_ms(l.get("created_at"))})
        repo.create_tag(name, target, pygit2.GIT_OBJECT_COMMIT, sig,
                        f"{l.get('comment')}\n\nOverleaf label at v{l.get('version')}\n")
        log(f"  label -> tag {name}")
        n += 1
    return n

def _iso_ms(iso):
    try:
        import datetime
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000
    except (AttributeError, ValueError):
        return None

def replay_history(project_dir, cfg, cookie, updates, fetch_zip=None,
                   trickle=False):
    if fetch_zip is None:
        try:
            return replay_history_api(project_dir, cfg, cookie, updates)
        except SyncError as e:
            msg = str(e)
            if not ("HTTP 404" in msg and ("/diff" in msg or "/filetree" in msg)):
                raise
            print(f"history API unavailable ({e}) — using version zips "
                  f"(rate-limited).")
    return replay_history_zips(project_dir, cfg, cookie, updates, fetch_zip,
                               trickle)

def replay_history_zips(project_dir, cfg, cookie, updates, fetch_zip=None,
                        trickle=False):
    """Replay a list of updates (oldest->newest) into the mirror, downloading a
    version zip per update (serial + adaptive pacing, polite to the server).
    Each version is committed as soon as its zip arrives, so an interrupted run
    resumes from the mirror tip. Returns #new commits."""
    if fetch_zip is None:
        fetch_zip = lambda v: download_version_zip(cfg, cookie, v)
    repo = open_mirror(project_dir)
    tip_toV = mirror_tip_toV(repo)
    todo = [u for u in updates if tip_toV is None or u["toV"] > tip_toV]
    if not todo:
        return 0
    pacer = _ReplayPacer(cfg, trickle=trickle)
    n = 0
    for u in todo:
        if n and pacer.delay:
            time.sleep(pacer.delay)
        data = pacer.fetch(fetch_zip, u["toV"])
        apply_version_zip(repo, project_dir, data, u)
        n += 1
        if n % 25 == 0 or n == len(todo):
            print(f"  replayed {n}/{len(todo)} (v{u['fromV']}→v{u['toV']})")
    return n

def snapshot_latest(project_dir, cfg, cookie, args):
    """Fallback when the updates API is unavailable: one snapshot commit from
    the latest zip. Returns (made_commit: bool, content_hash or None)."""
    repo = open_mirror(project_dir)
    data = read_zip_input(getattr(args, "from_zip", None)) or download_latest_zip(cfg, cookie)
    stage = os.path.join(project_dir, STAGE_DIR)
    try:
        extract_zip(data, stage)
        h = content_hash(stage)
        state = load_state(project_dir)
        if not getattr(args, "force", False) and h == state.get("last_content_hash"):
            return False, None
        tree = build_tree_from_dir(repo, stage)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        commit_snapshot(repo, tree, f"Overleaf snapshot ({ts})")
        print(f"snapshot committed on mirror: {str(repo.head.target)[:12]} ({ts})")
        return True, h
    finally:
        shutil.rmtree(stage, ignore_errors=True)

# --------------------------------------------------------------------------- state / lock

def load_state(project_dir):
    p = os.path.join(project_dir, STATE_FILE)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}

def save_state(project_dir, state):
    os.makedirs(os.path.join(project_dir, ".git"), exist_ok=True)
    with open(os.path.join(project_dir, STATE_FILE), "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")

class Lock:
    def __init__(self, project_dir):
        os.makedirs(os.path.join(project_dir, ".git"), exist_ok=True)
        self.path = os.path.join(project_dir, LOCK_FILE)
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fd.close()
            raise FgError("another sync/watch is already running for this project "
                          f"(lock {self.path} is held).")
    def release(self):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self.fd.close()
        except OSError:
            pass
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.release()

# --------------------------------------------------------------------------- working repo plumbing

def ensure_git_repo(project_dir):
    if not os.path.isdir(os.path.join(project_dir, ".git")):
        raise FgError("no git repository here. Run `init` first.")

def ensure_identity(project_dir):
    for key in ("user.name", "user.email"):
        p = git(project_dir, "config", key, check=False)
        if p.returncode != 0 or not p.stdout.strip():
            raise FgError(
                f"git identity not configured ({key}). Set it once, e.g.:\n"
                f"  git config --global {key} \"Your Name\" / \"you@example.com\"")

def ensure_origin(project_dir):
    """Make sure the working repo has 'origin' pointing at the mirror."""
    mirror = mirror_path(project_dir)
    p = git(project_dir, "remote", "get-url", "origin", check=False)
    if p.returncode == 0:
        if p.stdout.strip() != mirror:
            git(project_dir, "remote", "set-url", "origin", mirror)
    else:
        git(project_dir, "remote", "add", "origin", mirror)

def working_branch(project_dir):
    return git(project_dir, "branch", "--show-current").stdout.strip()

def merge_origin(project_dir, auto_stash):
    """git merge origin/main into the current branch, auto-stashing local
    changes first if allowed. Raises SyncError on conflict."""
    st = git(project_dir, "status", "--porcelain").stdout
    dirty = bool(st.strip())
    stash_ref = None
    if dirty:
        if not auto_stash:
            raise FgError("working tree has uncommitted changes; commit or stash "
                          "them first, or set auto_stash: true in config.json.")
        msg = f"{APP_NAME}: pre-merge stash {time.strftime('%Y-%m-%d %H:%M:%S')}"
        git(project_dir, "stash", "push", "-u", "-m", msg)
        stash_ref = "stash@{0}"
        print(f"stashed {len(st.strip().splitlines())} local change(s) before "
              f"merging ({msg})")
    p = git(project_dir, "merge", "origin/main", "--no-edit", "--no-ff", "-m",
            f"Merge Overleaf changes ({time.strftime('%Y-%m-%d %H:%M:%S')})",
            check=False)
    if p.returncode != 0:
        conflicts = git(project_dir, "diff", "--name-only", "--diff-filter=U",
                        check=False).stdout.split()
        detail = ", ".join(conflicts[:15]) if conflicts else "(none listed)"
        extra = ("\nYour local changes are stashed — after resolving, run "
                 "`git stash pop` to bring them back.") if stash_ref else ""
        raise SyncError(
            f"merge conflicts in {len(conflicts)} file(s): {detail}\n"
            f"Resolve them (edit the files), then `git add` each and `git commit`, "
            f"or `git merge --abort` to undo.{extra}")
    if stash_ref:
        pop = git(project_dir, "stash", "pop", check=False)
        if pop.returncode != 0:
            print(f"WARNING: `git stash pop` had trouble re-applying your local "
                  f"changes:\n{pop.stderr.strip()[:400]}\nrun `git stash pop` "
                  f"manually.")

# --------------------------------------------------------------------------- project .gitignore

PROJECT_GITIGNORE = [
    "# overleaf-fakegit local files",
    ".gitignore",
    "cookie.txt",
    "config.json",
    "stage/",
    ".overleaf/",
    "*.zip",
]

def write_project_gitignore(project_dir):
    path = os.path.join(project_dir, ".gitignore")
    existing = []
    if os.path.exists(path):
        with open(path) as f:
            existing = f.read().splitlines()
    new = [l for l in PROJECT_GITIGNORE if l and l not in existing]
    if new:
        with open(path, "a") as f:
            if existing:
                f.write("\n")
            f.write("\n".join(new) + "\n")

# --------------------------------------------------------------------------- local updates-dir (debug/offline)

def read_zip_input(path):
    if not path:
        return None
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(ZIP_MAGIC):
        raise FgError(f"--from-zip {path} is not a zip file")
    return data

# --------------------------------------------------------------------------- project discovery / interactive

PROJECT_ID_RE = re.compile(r"[0-9a-fA-F]{24}")

def fetch_projects(cfg, cookie):
    """Fetch the user's project list from the dashboard page (embedded JSON in
    the ol-prefetchedProjectsBlob meta tag)."""
    data = http_get(cfg, cookie, "/project", timeout=60)
    text = data.decode("utf-8", "replace")
    m = re.search(
        r'<meta name="ol-prefetchedProjectsBlob" data-type="json" content="([^"]+)"',
        text)
    if not m:
        raise FgError(
            "could not find the project list on the dashboard page (this "
            "instance may be too old). Use --project <id> instead.")
    try:
        obj = json.loads(html.unescape(m.group(1)))
    except (ValueError, AttributeError) as e:
        raise FgError(f"could not parse the project list: {e}")
    return obj.get("projects") or []

def _prompt(msg, default=None):
    if default is not None:
        msg = f"{msg} [{default}]: "
    else:
        msg = f"{msg}: "
    try:
        val = input(msg).strip()
    except EOFError:
        raise FgError(
            f"no input available for '{msg.strip()}' — run interactively or "
            f"pass the value as a flag.")
    return val or (default if default is not None else "")

def _choose(msg, items):
    """Interactive picker. items: list of (value, label). Accepts a number or
    a value pasted directly."""
    for i, (_, label) in enumerate(items, 1):
        print(f"  {i}. {label}")
    while True:
        sel = _prompt(f"{msg} (1-{len(items)} or paste a value)")
        for value, _ in items:
            if sel == value:
                return value
        try:
            return items[int(sel) - 1][0]
        except (ValueError, IndexError):
            print(f"  enter a number 1-{len(items)} or paste a value.")

def _ensure_cookie(project_dir, cfg, cli_cookie=None, cli_cookie_file=None):
    """Resolve the cookie, prompting to paste one if missing (saved so future
    runs work)."""
    try:
        return resolve_cookie(project_dir, cfg, cli_cookie, cli_cookie_file)
    except FgError:
        val = _prompt("Paste your overleaf.sid (or the full Cookie header)")
        if not val:
            raise FgError("no cookie — cannot continue.")
        path = os.path.join(project_dir, cfg.get("cookie_file") or DEFAULT_COOKIE_FILE)
        with open(path, "w") as f:
            f.write(val + "\n")
        print(f"saved cookie to {path}")
        return normalize_cookie(val)

def _resolve_project(cfg, cookie, name_or_id, interactive=True):
    """Turn a project argument (24-hex id, or a name substring) into a project
    id. With an empty name_or_id, shows an interactive picker."""
    if PROJECT_ID_RE.fullmatch(name_or_id or ""):
        return name_or_id
    projects = fetch_projects(cfg, cookie)
    if not projects:
        raise FgError("no projects found in this account.")
    matches = []
    if name_or_id:
        matches = [p for p in projects
                   if name_or_id.lower() in (p.get("name") or "").lower()]
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            print(f"{len(matches)} projects match '{name_or_id}':")
        elif interactive:
            print(f"no project matches '{name_or_id}' — showing all:")
    pool = matches or projects
    if not interactive:
        raise FgError(
            f"'{name_or_id}' does not uniquely identify a project "
            f"({len(matches)} match(es)). Run `projects` to list them, or pass "
            f"a 24-hex project id.")
    items = [(p.get("id") or "", f"{p.get('name')}  ({p.get('accessLevel')}, "
             f"updated {str(p.get('lastUpdated'))[:10]})")
             for p in sorted(pool, key=lambda p: p.get("name") or "")]
    return _choose("Which project", items)

def _updates_from_dir(updates_dir):
    """Debug/offline mode: read the updates list + version zips from a local
    directory instead of the network."""
    with open(os.path.join(updates_dir, "updates.json")) as f:
        obj = json.load(f)
    updates = obj.get("updates", [])
    updates.reverse()                      # newest-first in file -> oldest first
    return updates

def _version_zip_from_dir(updates_dir, version):
    with open(os.path.join(updates_dir, f"v{version}.zip"), "rb") as f:
        return f.read()

# --------------------------------------------------------------------------- realtime (socket.io v1)

EDIT_EVENTS = {
    "otUpdateApplied", "applyOtUpdate", "reciveNewDocOps", "recieveNewDocOps",
    "project:edited", "docUpdated", "joinProjectResponse",
}

def split_socketio_payload(resp):
    """Decode a socket.io 1.x xhr-polling payload: packets are separated by
    \\ufffd<len>\\ufffd. Returns a list of packet strings."""
    out = []
    i = 0
    n = len(resp)
    while i < n:
        if resp[i] == "\ufffd":
            j = resp.find("\ufffd", i + 1)
            if j < 0:
                out.append(resp[i:])
                break
            ln = int(resp[i + 1:j])
            out.append(resp[j + 1:j + 1 + ln])
            i = j + 1 + ln
        else:
            out.append(resp[i:])
            break
    return out

class RealtimeListener:
    """socket.io v1 xhr-polling client for the old ShareLaTeX realtime service.

    The handshake MUST include ?projectId= — that binds the session to the
    project room and the first poll delivers joinProjectResponse (the current
    project state). Subsequent polls long-poll for events (otUpdateApplied on
    edits, clientTracking presence, 2:: heartbeats)."""
    def __init__(self, cfg, cookie):
        self.base = (cfg["url"] or "").rstrip("/")
        self.pid = cfg["project"]
        self.cookie = cookie
        self.sid = None

    def _req(self, path, data=None, timeout=70):
        url = f"{self.base}{path}"
        hdrs = {"Cookie": self.cookie,
                "Origin": self.base,
                "Referer": f"{self.base}/project/{self.pid}",
                "Content-Type": "text/plain;charset=UTF-8",
                "User-Agent": f"{APP_NAME}/1.0"}
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return None, None

    def _t(self):
        return str(int(time.time() * 1000))

    def _poll_url(self):
        return f"/socket.io/1/xhr-polling/{self.sid}?projectId={self.pid}&t={self._t()}"

    def connect(self):
        """Handshake + open + first poll (joinProjectResponse). Returns the
        first payload or None on failure."""
        self.sid = None
        st, body = self._req(
            f"/socket.io/1/?projectId={self.pid}&t={self._t()}", timeout=20)
        if not body or ":" not in body:
            raise FgError("realtime handshake failed")
        self.sid = body.split(":")[0]
        self._req(self._poll_url())                       # open POST (connect)
        st, resp = self._req(self._poll_url(), timeout=30)
        return resp or None

    def poll(self, timeout=70):
        if not self.sid:
            raise FgError("not connected")
        st, resp = self._req(self._poll_url(), timeout=timeout)
        if resp is None:
            self.sid = None
            raise FgError("realtime poll failed")
        return resp

    def close(self):
        if self.sid:
            self._req(self._poll_url(), data=b"0::", timeout=10)   # disconnect
        self.sid = None

    def join(self):
        """Connect and return the joinProjectResponse project dict (the full
        folder tree with doc/file/folder ids), then disconnect."""
        try:
            resp = self.connect()
            for _ in range(3):
                for pkt in split_socketio_payload(resp or ""):
                    if not pkt.startswith("5:::"):
                        continue
                    try:
                        obj = json.loads(pkt[4:])
                    except ValueError:
                        continue
                    if obj.get("name") == "joinProjectResponse":
                        return obj["args"][0]["project"]
                resp = self.poll(timeout=30)
        finally:
            self.close()
        raise SyncError("realtime: no joinProjectResponse (cannot read the "
                        "project's file tree)")

# --------------------------------------------------------------------------- write API (push)

def fetch_csrf(cfg, cookie):
    page = http_get(cfg, cookie, f"/project/{cfg['project']}").decode("utf-8", "replace")
    m = re.search(r'<meta name="ol-csrfToken" content="([^"]+)"', page)
    if not m:
        raise SyncError("could not find the CSRF token on the editor page")
    return html.unescape(m.group(1))

def http_send(cfg, cookie, csrf, method, path, data=None, content_type=None,
              timeout=120):
    """Authenticated write request (CSRF-protected). Returns parsed JSON (or
    {} for an empty/non-JSON body)."""
    base = cfg["url"].rstrip("/")
    headers = {"Cookie": cookie, "User-Agent": f"{APP_NAME}/1.0",
               "X-Csrf-Token": csrf, "Origin": base, "Accept": "application/json",
               "Referer": f"{base}/project/{cfg['project']}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if urllib.parse.urlsplit(resp.geturl()).path.rstrip("/") == "/login":
                raise AuthError(COOKIE_EXPIRED_MSG)
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace").strip()
        if e.code == 401:
            raise AuthError(f"HTTP 401 — {COOKIE_EXPIRED_MSG}")
        raise SyncError(f"{method} {path}: HTTP {e.code} {detail}")
    except urllib.error.URLError as e:
        raise SyncError(f"network error: {e.reason}")
    try:
        return json.loads(body) if body else {}
    except ValueError:
        return {}

def _multipart(field, filename, data):
    boundary = "----fakegit" + os.urandom(12).hex()
    quoted = filename.replace("\\", "\\\\").replace('"', '\\"')
    # newer Overleaf reads the name from a `name` field, older ones from the
    # file part's filename — send both
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="name"\r\n\r\n'
            f"{filename}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{quoted}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    return head + data + f"\r\n--{boundary}--\r\n".encode(), \
        f"multipart/form-data; boundary={boundary}"

def project_entities(project):
    """Flatten a joinProjectResponse project into {path: (type, id)}; the root
    folder is under ''. type is doc / file / folder."""
    out = {}
    def walk(folder, prefix):
        for d in folder.get("docs") or []:
            out[prefix + d["name"]] = ("doc", d["_id"])
        for f in folder.get("fileRefs") or []:
            out[prefix + f["name"]] = ("file", f["_id"])
        for sub in folder.get("folders") or []:
            p = prefix + sub["name"]
            out[p] = ("folder", sub["_id"])
            walk(sub, p + "/")
    root = project["rootFolder"][0]
    out[""] = ("folder", root["_id"])
    walk(root, "")
    return out

def push_changes(cfg, cookie, new_paths, upserts, deletes, log=print,
                 renames=()):
    """Make the Overleaf project match a new tree: rename/move `renames`
    ([(old, new)] — keeps the entity and its history), delete `deletes`
    (paths), upload `upserts` ({path: bytes}, create or replace in place),
    and drop folders left without any file in `new_paths`."""
    ents = project_entities(RealtimeListener(cfg, cookie).join())
    csrf = fetch_csrf(cfg, cookie)
    pid = cfg["project"]

    def under(path, folder):
        return path.startswith(folder + "/")

    def post_json(path, obj):
        return http_send(cfg, cookie, csrf, "POST", path,
                         json.dumps(obj).encode(), "application/json")

    def folder_id(path):
        if path in ents:
            t, eid = ents[path]
            if t != "folder":
                raise SyncError(f"cannot create folder {path}/: a file has that name")
            return eid
        parent, _, name = path.rpartition("/")
        r = http_send(cfg, cookie, csrf, "POST", f"/project/{pid}/folder",
                      json.dumps({"name": name,
                                  "parent_folder_id": folder_id(parent)}).encode(),
                      "application/json")
        if not r.get("_id"):
            raise SyncError(f"creating folder {path}/ failed: {r}")
        ents[path] = ("folder", r["_id"])
        log(f"  created folder {path}/")
        return r["_id"]

    for old, new in renames:
        if old not in ents:
            raise SyncError(f"{old} is not on Overleaf any more — pull first")
        t, eid = ents.pop(old)
        (op, _, oname), (np_, _, nname) = old.rpartition("/"), new.rpartition("/")
        if op != np_:
            post_json(f"/project/{pid}/{t}/{eid}/move", {"folder_id": folder_id(np_)})
        if oname != nname:
            post_json(f"/project/{pid}/{t}/{eid}/rename", {"name": nname})
        ents[new] = (t, eid)
        log(f"  renamed {old} -> {new}")
    renamed_away = [old for old, _ in renames]

    # folders that held deleted/moved files and keep nothing: delete the topmost
    dead = sorted(p for p, (t, _) in ents.items()
                  if t == "folder" and p
                  and any(under(d, p) for d in deletes + renamed_away)
                  and not any(under(n, p) for n in new_paths))
    dead = [p for p in dead if not any(under(p, q) for q in dead if q != p)]
    for path in deletes:
        if path in ents and not any(under(path, f) for f in dead):
            t, eid = ents[path]
            http_send(cfg, cookie, csrf, "DELETE", f"/project/{pid}/{t}/{eid}")
            log(f"  deleted {path}")
    for path in dead:
        http_send(cfg, cookie, csrf, "DELETE",
                  f"/project/{pid}/folder/{ents[path][1]}")
        log(f"  deleted folder {path}/")

    for path, data in upserts.items():
        parent, _, name = path.rpartition("/")
        body, ctype = _multipart("qqfile", name, data)
        r = http_send(cfg, cookie, csrf, "POST",
                      f"/Project/{pid}/upload?folder_id={folder_id(parent)}",
                      body, ctype)
        if not r.get("success"):
            raise SyncError(f"uploading {path} failed: {r}")
        log(f"  {'updated' if path in ents else 'added'} {path}")

# --------------------------------------------------------------------------- push hook (receive-pack on the mirror)

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ZERO_SHA = "0" * 40

def install_push_hook(project_dir):
    """Make the mirror accept pushes to main (fast-forward only); the
    pre-receive hook uploads the change to Overleaf before the ref moves."""
    mirror = mirror_path(project_dir)
    hook = os.path.join(mirror, "hooks", "pre-receive")
    os.makedirs(os.path.dirname(hook), exist_ok=True)
    with open(hook, "w") as f:
        f.write("#!/bin/sh\n"
                f"exec '{sys.executable}' '{os.path.abspath(__file__)}' "
                f"--dir '{os.path.abspath(project_dir)}' pre-receive\n")
    os.chmod(hook, 0o755)
    conf = pygit2.Repository(mirror).config    # not `git -C`: GIT_DIR may be set
    conf["receive.denyNonFastForwards"] = True
    conf["receive.denyDeletes"] = True

def _hook_git(*args):
    # inherits GIT_DIR + quarantine env from receive-pack, so pushed objects
    # are visible before the ref is updated
    p = subprocess.run(["git", *args], capture_output=True)
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: "
                       f"{p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout

def cmd_pre_receive(args):
    project_dir = args.dir
    cfg = load_config(project_dir)
    lines = [l.split() for l in sys.stdin.read().splitlines() if l.strip()]
    tags = [l for l in lines if l[2].startswith("refs/tags/")]
    lines = [l for l in lines if l not in tags]
    for old, new, ref in lines:
        if ref != MAIN_REF:
            raise FgError(f"only {MAIN_REF} and tags can be pushed (Overleaf "
                          f"has no branches)")
        if new == ZERO_SHA:
            raise FgError("deleting main is not allowed")
    for old, new, ref in tags:
        if new == ZERO_SHA or old != ZERO_SHA:
            raise FgError(f"{ref}: tags can only be created (Overleaf labels "
                          f"can't be moved or deleted from git)")
    cookie = resolve_cookie(project_dir, cfg)
    state = load_state(project_dir)
    for old, new, ref in lines:
        # Overleaf must still be at the state the mirror holds, or we'd
        # overwrite edits we haven't seen
        newest, _ = fetch_updates(cfg, cookie, min_count=1)
        newest_toV = newest[0]["toV"] if newest else None
        known = state.get("last_toV")
        if known is not None and newest_toV is not None and newest_toV != known:
            raise FgError(f"Overleaf has changed since your last fetch "
                          f"(v{known} → v{newest_toV}). Pull first, then push again.")
        base = EMPTY_TREE if old == ZERO_SHA else old
        # -M: git's rename detection -> Overleaf rename/move (keeps history)
        diff = _hook_git("diff-tree", "-r", "-z", "-M", base, new).split(b"\0")
        upserts, deletes, renames = {}, [], []
        i = 0
        while i < len(diff) - 1:
            # ":<old mode> <new mode> <old sha> <new sha> <status>" \0 path [\0 new path]
            _, mode, old_sha, sha, status = diff[i].decode().split()
            path = diff[i + 1].decode("utf-8", "surrogateescape")
            i += 2
            if status.startswith("R"):
                renames.append((path, diff[i].decode("utf-8", "surrogateescape")))
                path = renames[-1][1]
                i += 1
                if sha == old_sha:
                    continue
            elif status == "D":
                deletes.append(path)
                continue
            if mode in ("120000", "160000"):
                raise FgError(f"{path}: symlinks/submodules can't be pushed to Overleaf")
            upserts[path] = _hook_git("cat-file", "blob", sha)
        if not upserts and not deletes and not renames:
            print("overleaf: nothing to upload (same content).")
            continue
        new_paths = set(_hook_git("ls-tree", "-r", "-z", "--name-only", new)
                        .decode("utf-8", "surrogateescape").split("\0")) - {""}
        print(f"overleaf: uploading {len(upserts)} file(s), deleting "
              f"{len(deletes)}, renaming {len(renames)}…", flush=True)
        try:
            push_changes(cfg, cookie, new_paths, upserts, deletes,
                         log=lambda m: print(m, flush=True), renames=renames)
        except FgError as e:
            raise FgError(f"{e}\nOverleaf may be partially updated — pull, "
                          f"then push again.")
        # our own edits are new Overleaf versions: mark them as seen so the
        # next fetch doesn't replay them back as duplicate commits
        newest, _ = fetch_updates(cfg, cookie, min_count=1)
        if newest:
            state["last_toV"] = newest[0]["toV"]
        state["last_push"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(project_dir, state)
        print("overleaf: push applied.")
    if tags:
        # a tag labels the Overleaf state, so it must point at main's tip
        # (as of this push)
        main_tip = lines[-1][1] if lines else _hook_git(
            "rev-parse", MAIN_REF).decode().strip()
        newest, _ = fetch_updates(cfg, cookie, min_count=1)
        csrf = fetch_csrf(cfg, cookie)
        for _, new, ref in tags:
            target = _hook_git("rev-parse", new + "^{commit}").decode().strip()
            if target != main_tip:
                raise FgError(f"{ref}: a tag must point at main's tip (the "
                              f"current Overleaf state) to become a label")
            name = ref[len("refs/tags/"):]
            msg = ""                    # annotated tag: its subject line
            if _hook_git("cat-file", "-t", new).decode().strip() == "tag":
                body = _hook_git("cat-file", "tag", new).decode("utf-8", "replace")
                msg = body.partition("\n\n")[2].strip().partition("\n")[0]
            comment = msg or (name[len(LABEL_TAG_PREFIX):]
                              if name.startswith(LABEL_TAG_PREFIX) else name)
            http_send(cfg, cookie, csrf, "POST", f"/project/{cfg['project']}/labels",
                      json.dumps({"comment": comment,
                                  "version": newest[0]["toV"]}).encode(),
                      "application/json")
            print(f"overleaf: label '{comment}' created at v{newest[0]['toV']}")

def _watch_realtime(project_dir, args, cfg):
    """Event-driven watch: keep a socket.io v1 connection to the project and
    sync as soon as an edit event arrives (with an interval safety net)."""
    cookie = resolve_cookie(project_dir, cfg, getattr(args, "cookie", None),
                            getattr(args, "cookie_file", None))
    listener = RealtimeListener(cfg, cookie)
    interval = args.interval or cfg.get("interval") or 300
    last_interval_sync = 0.0
    print(f"realtime watch: {os.path.abspath(project_dir)} — listening for "
          f"Overleaf edits (interval safety net {interval}s)")
    while True:
        try:
            if listener.sid is None:
                resp = listener.connect()
                if resp is None:
                    raise FgError("realtime connect failed")
                print("realtime connected — will sync on edits.")
                events = ["<connect>"]
            else:
                resp = listener.poll(timeout=70)
                events = []
                for pkt in split_socketio_payload(resp):
                    pkt = pkt.strip()
                    if not pkt:
                        continue
                    if pkt.startswith("7:::"):
                        raise FgError(f"realtime error frame: {pkt[:40]}")
                    if not pkt.startswith("5:::"):
                        continue                       # 2:: heartbeat etc.
                    body = pkt[4:]
                    name = "<unknown>"
                    if body.startswith("{"):
                        try:
                            obj = json.loads(body)
                            name = obj.get("name", "<unknown>")
                        except ValueError:
                            pass
                    if name in EDIT_EVENTS:
                        events.append(name)
            if events:
                print(f"[{time.strftime('%H:%M:%S')}] edit event(s): "
                      f"{', '.join(events)} — syncing")
                do_sync(project_dir, args, cfg)
                last_interval_sync = time.time()
            elif time.time() - last_interval_sync >= interval:
                print(f"[{time.strftime('%H:%M:%S')}] interval sync")
                do_sync(project_dir, args, cfg)
                last_interval_sync = time.time()
        except FgError as e:
            print(f"[{time.strftime('%H:%M:%S')}] {e} — reconnecting in 5s")
            listener.sid = None
            time.sleep(5)
        except KeyboardInterrupt:
            print("\nstopped.")
            return

# --------------------------------------------------------------------------- commands

def cmd_init(args):
    project_dir = args.dir
    os.makedirs(project_dir, exist_ok=True)
    cfg = load_config(project_dir)
    if args.url:
        cfg["url"] = args.url
    elif not cfg.get("url"):
        cfg["url"] = _prompt("Overleaf instance URL", "https://overleaf.ktu.edu.tr")
    if not cfg.get("url"):
        raise FgError("no instance URL. Pass --url https://overleaf.example.edu")
    cookie = _ensure_cookie(project_dir, cfg, args.cookie, args.cookie_file)
    project_arg = args.project or cfg.get("project")
    cfg["project"] = _resolve_project(cfg, cookie, project_arg or "")
    save_config(project_dir, cfg)
    write_project_gitignore(project_dir)

    create_mirror(project_dir)
    install_push_hook(project_dir)
    repo = open_mirror(project_dir)

    # --- replay history into the mirror
    snapshot_hash = None
    seed_toV = None
    if args.updates_dir:
        updates = _updates_from_dir(args.updates_dir)
        fetch_zip = lambda v: _version_zip_from_dir(args.updates_dir, v)
    else:
        try:
            # default: the whole history (history API, not rate-limited);
            # --max-updates N for the last N changes, 0 for a snapshot only
            updates = fetch_all_updates(cfg, cookie, after_toV=None,
                                        max_updates=args.max_updates)
        except SyncError as e:
            if _is_updates_error(e):
                print(f"change list unavailable ({e}) — seeding the mirror with "
                      f"a snapshot of the latest version instead.")
                updates = []
            else:
                raise
        fetch_zip = None
    if updates:
        print(f"replaying {len(updates)} Overleaf change(s) into the mirror "
              f"(one commit each; first run only)…")
        n = replay_history(project_dir, cfg, cookie, updates, fetch_zip)
        print(f"mirror history: {n} commit(s) replayed "
              f"→ {str(repo.head.target)[:12]}")
        sync_labels(project_dir, cfg, cookie)
    if repo.head_is_unborn:
        # no change list (or all filtered) but nothing in the mirror: seed it
        print("seeding mirror with a snapshot of the latest version.")
        _, snapshot_hash = snapshot_latest(project_dir, cfg, cookie, args)
        # record the current version so later syncs only pull NEW changes
        # instead of trying to backfill the whole history (rate-limited)
        try:
            newest, _ = fetch_updates(cfg, cookie, min_count=1)
            seed_toV = newest[0]["toV"] if newest else None
        except FgError:
            seed_toV = None

    # --- working repo: clone the mirror
    if not os.path.isfile(os.path.join(project_dir, ".git", "HEAD")):
        p = git(project_dir, "init", "-b", "main", check=False)
        if p.returncode != 0:
            git(project_dir, "init")
            git(project_dir, "checkout", "-b", "main")
    ensure_identity(project_dir)
    ensure_origin(project_dir)
    git(project_dir, "fetch", "origin")
    if not git(project_dir, "rev-parse", "-q", "--verify", "HEAD",
               check=False).stdout.strip():
        p = git(project_dir, "checkout", "-B", "main", "origin/main", check=False)
        if p.returncode != 0:
            # leftover files in the folder that differ from the mirror state:
            # init imports the mirror state over them
            git(project_dir, "checkout", "-f", "-B", "main", "origin/main")

    head = git(project_dir, "rev-parse", "HEAD").stdout.strip()
    state = load_state(project_dir)
    if snapshot_hash is not None:
        state["last_content_hash"] = snapshot_hash
    state.update({
        "last_toV": mirror_tip_toV(repo) or seed_toV or state.get("last_toV"),
        "last_sync": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_commit": head,
    })
    save_state(project_dir, state)
    print(f"initialized: project {cfg['project']} @ {cfg['url']}")
    print(f"  mirror:  {mirror_path(project_dir)}")
    print(f"  working: {os.path.abspath(project_dir)} (branch: "
          f"{working_branch(project_dir)})")
    print("next steps:\n"
          "  edit files and commit locally:  git add -A && git commit -m '...'\n"
          f"  pull Overleaf changes:          python3 {os.path.abspath(__file__)} "
          f"sync --dir {project_dir}\n"
          f"  sync every 5 min:               python3 {os.path.abspath(__file__)} "
          f"watch --dir {project_dir}\n"
          f"  serve read-only:                python3 {os.path.abspath(__file__)} "
          f"serve --dir {project_dir}")

def do_sync(project_dir, args, cfg):
    cookie = resolve_cookie(project_dir, cfg, getattr(args, "cookie", None),
                            getattr(args, "cookie_file", None))
    repo = open_mirror(project_dir)
    tip_before = mirror_tip_toV(repo)
    snapshot_hash = None
    backfill = getattr(args, "backfill", False)
    newest_toV = None

    if getattr(args, "updates_dir", None):
        updates = _updates_from_dir(args.updates_dir)
        fetch_zip = lambda v: _version_zip_from_dir(args.updates_dir, v)
        replay_history(project_dir, cfg, cookie, updates, fetch_zip,
                       trickle=backfill)
        newest_toV = updates[-1]["toV"] if updates else None
    else:
        try:
            after = None if backfill else load_state(project_dir).get("last_toV")
            updates = fetch_all_updates(cfg, cookie, after_toV=after,
                                        max_updates=None)
            newest_toV = updates[-1]["toV"] if updates else None
            if getattr(args, "dry_run", False):
                print(f"would replay {len(updates)} change(s)")
                return
            replay_history(project_dir, cfg, cookie, updates, trickle=backfill)
        except SyncError as e:
            if "throttl" in str(e):
                print(f"warning: {e}")
            elif _is_updates_error(e):
                print(f"updates API unavailable ({e}) — falling back to snapshot.")
                _, snapshot_hash = snapshot_latest(project_dir, cfg, cookie, args)
            else:
                raise
    if not getattr(args, "updates_dir", None):
        sync_labels(project_dir, cfg, cookie)
    tip_after = mirror_tip_toV(repo)
    changed = tip_after is not None and tip_after != tip_before
    if backfill:
        # Backfill appends OLD history below the tip, so the mirror tip's tree
        # is old until the replay catches up. Only merge into the working repo
        # once the mirror reaches the newest version (trees identical -> clean).
        if tip_after is not None and newest_toV is not None and tip_after >= newest_toV:
            print("history backfill complete — merging into the working repo.")
            changed = True
        else:
            if changed:
                print(f"history backfill in progress ({tip_after}/{newest_toV}) — "
                      f"mirror updated; working repo untouched until backfill "
                      f"completes.")
            else:
                print(f"history backfill: at {tip_after}/{newest_toV}, "
                      f"throttled or idle — will resume next sync.")
            changed = False
    if not changed:
        if not backfill:
            print("no changes on Overleaf since last sync.")
        return

    # merge into the working repo
    ensure_git_repo(project_dir)
    branch = working_branch(project_dir)
    if not branch:
        raise FgError("detached HEAD — check out your working branch first.")
    ensure_origin(project_dir)
    git(project_dir, "fetch", "origin")
    merge_origin(project_dir,
                 cfg.get("auto_stash", True) and not getattr(args, "no_stash", False))
    head = git(project_dir, "rev-parse", "HEAD").stdout.strip()
    state = load_state(project_dir)
    if snapshot_hash is not None:
        state["last_content_hash"] = snapshot_hash
    state.update({
        "last_toV": mirror_tip_toV(repo) or state.get("last_toV"),
        "last_sync": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_commit": head,
    })
    save_state(project_dir, state)
    print("sync done — working tree is up to date with Overleaf.")

def cmd_sync(args):
    project_dir = args.dir
    ensure_mirror(project_dir)
    ensure_git_repo(project_dir)
    cfg = load_config(project_dir)
    install_push_hook(project_dir)      # mirrors created before push support
    with Lock(project_dir):
        do_sync(project_dir, args, cfg)

def cmd_watch(args):
    project_dir = args.dir
    ensure_mirror(project_dir)
    ensure_git_repo(project_dir)
    cfg = load_config(project_dir)
    interval = args.interval or cfg.get("interval") or 300
    if interval <= 0:
        raise FgError("interval must be > 0")
    realtime = args.realtime or cfg.get("realtime", False)
    if realtime:
        with Lock(project_dir):
            _watch_realtime(project_dir, args, cfg)
        return
    print(f"watching {os.path.abspath(project_dir)} — syncing every {interval}s "
          f"(Ctrl-C to stop)", flush=True)
    with Lock(project_dir):
        while True:
            try:
                do_sync(project_dir, args, cfg)
            except FgError as e:
                print(f"[{time.strftime('%H:%M:%S')}] {e}", flush=True)
            except KeyboardInterrupt:
                print("\nstopped.")
                return
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\nstopped.")
                return

def _local_ipv4s():
    """IPv4 addresses of this host (loopback + interfaces), for printing the
    addresses a git daemon bound to all interfaces accepts connections on."""
    addrs = ["127.0.0.1"]
    found = []
    try:                                     # hostname -I (coreutils)
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                             timeout=5)
        if out.returncode == 0:
            found += out.stdout.split()
    except Exception:
        pass
    if not found:
        try:                                 # ip -4 addr (iproute2)
            out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope",
                                  "global"], capture_output=True, text=True,
                                 timeout=5)
            for line in out.stdout.splitlines():
                for p in line.split():
                    if "/" in p and p.count(".") == 3:
                        found.append(p.split("/")[0])
        except Exception:
            pass
    if not found:
        try:                                 # outbound-route trick (UDP, no send)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            found.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    for a in found:
        if ":" not in a and a not in addrs:  # IPv4 only (fe80 etc. are noise)
            addrs.append(a)
    return addrs

def _git_url(addr, port):
    if ":" in addr:                      # IPv6 needs brackets
        return f"git://[{addr}]:{port}/mirror.git"
    return f"git://{addr}:{port}/mirror.git"

def _wait_listening(port, timeout=6):
    """Wait until something accepts connections on 127.0.0.1:port."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False

def cmd_serve(args):
    project_dir = args.dir
    mirror = ensure_mirror(project_dir)
    port = args.port or 9418
    listen = args.listen
    overleaf_dir = os.path.join(project_dir, OVERLEAF_DIR)
    os.makedirs(overleaf_dir, exist_ok=True)
    pid_file = os.path.join(overleaf_dir, "daemon.pid")
    if args.stop:
        if os.path.exists(pid_file):
            with open(pid_file) as f:
                pid = int(f.read().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"stopped git daemon (pid {pid})")
            except ProcessLookupError:
                print("daemon not running (stale pid file)")
            os.remove(pid_file)
        else:
            print("no daemon running (no pid file)")
        return
    daemon = ["git", "daemon", "--reuseaddr",
              "--base-path=" + os.path.abspath(overleaf_dir),
              "--export-all", "--port=" + str(port), "--verbose"]
    if listen:
        daemon += ["--listen=" + listen]
    if args.background:
        daemon += ["--detach", "--pid-file=" + pid_file]
        subprocess.run(daemon, check=True)
    else:
        print(f"git daemon (read-only) on port {port} — Ctrl-C to stop")
        proc = subprocess.Popen(daemon)
    if not _wait_listening(port):
        print(f"warning: nothing is accepting connections on port {port} yet — "
              f"is the port already in use or is git daemon failing?",
              file=sys.stderr)
    # the addresses the daemon is bound to / reachable on
    if listen and listen not in ("0.0.0.0", "::", ""):
        addrs = [listen]
    else:
        addrs = _local_ipv4s()
    print("serving (read-only):")
    for a in addrs:
        print("  " + _git_url(a, port))
    print(f"  file://{os.path.abspath(mirror)}   (no server needed)")
    if args.background:
        pid = None
        if os.path.exists(pid_file):
            with open(pid_file) as f:
                pid = f.read().strip()
        print(f"daemon running (pid {pid}); stop with: serve --stop")
    else:
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            print("\nstopped.")

def cmd_status(args):
    project_dir = args.dir
    cfg = load_config(project_dir)
    print(f"dir:      {os.path.abspath(project_dir)}")
    print(f"url:      {cfg.get('url') or '(not set)'}")
    print(f"project:  {cfg.get('project') or '(not set)'}")
    print(f"cookie:   {'present' if _cookie_available(project_dir, cfg) else 'MISSING'}")
    state = load_state(project_dir)
    print(f"last sync: {state.get('last_sync', 'never')}  "
          f"(commit {str(state.get('last_commit', '-'))[:12]})")
    print(f"last replayed version: {state.get('last_toV', '-')}")
    if os.path.isdir(os.path.join(project_dir, ".git")):
        try:
            m = open_mirror(project_dir)
            if not m.head_is_unborn:
                n = sum(1 for _ in m.walk(m.head.target))
                print(f"mirror history: {n} commit(s), tip {str(m.head.target)[:12]}")
        except FgError:
            pass
        print("--- working repo history ---")
        p = git(project_dir, "log", "--oneline", "--decorate", "-8", check=False)
        print(p.stdout or "(no commits yet)")

def cmd_projects(args):
    """List the projects in the account (project discovery)."""
    project_dir = args.dir
    cfg = load_config(project_dir)
    if args.url:
        cfg["url"] = args.url
    elif not cfg.get("url"):
        cfg["url"] = _prompt("Overleaf instance URL", "https://overleaf.ktu.edu.tr")
    cookie = _ensure_cookie(project_dir, cfg, args.cookie, args.cookie_file)
    projects = fetch_projects(cfg, cookie)
    if args.search:
        projects = [p for p in projects
                    if args.search.lower() in (p.get("name") or "").lower()]
    if args.json:
        print(json.dumps(projects, indent=2))
        return
    if not projects:
        print("no projects" + (f" matching '{args.search}'" if args.search else ""))
        return
    for p in sorted(projects, key=lambda p: (p.get("name") or "").lower()):
        print(f"{p.get('id')}  {p.get('name')}  "
              f"({p.get('accessLevel')}, updated {str(p.get('lastUpdated'))[:10]})")

def remote_cfg(spec, git_config):
    """cfg + cookie for an `overleaf::<spec>` remote (spec: 24-hex id or a
    unique name). git_config(key) -> value or None."""
    tool_dir = os.path.dirname(os.path.abspath(__file__))
    tool_cfg = {}
    try:
        with open(os.path.join(tool_dir, CONFIG_NAME)) as f:
            tool_cfg = json.load(f)
    except (OSError, ValueError):
        pass
    url = (git_config("overleaf.url") or os.environ.get("OVERLEAF_URL")
           or tool_cfg.get("url") or "https://overleaf.ktu.edu.tr")
    cfg = dict(DEFAULTS, url=url.rstrip("/"))
    cookie = resolve_cookie(tool_dir, cfg, None, git_config("overleaf.cookie-file"))
    project = spec.split("/")[-1] if spec else ""
    if not PROJECT_ID_RE.fullmatch(project):
        project = _resolve_project(cfg, cookie, project, interactive=False)
    cfg["project"] = project
    return cfg, cookie

def project_context(project_dir):
    """(cfg, cookie) for a project folder: its config.json (init workflow) or
    an overleaf::<project> remote (clone workflow)."""
    cfg = load_config(project_dir)
    if cfg.get("project") and cfg.get("url"):
        return cfg, resolve_cookie(project_dir, cfg)
    remotes = git(project_dir, "remote", "-v", check=False).stdout.split()
    spec = next((r[len("overleaf::"):] for r in remotes if r.startswith("overleaf::")), None)
    if spec is None:
        raise FgError("not an Overleaf project folder (no config.json and no "
                      "overleaf::<project> remote).")
    def gc(key):
        out = git(project_dir, "config", "--get", key, check=False).stdout.strip()
        return out or None
    return remote_cfg(spec, gc)

# --------------------------------------------------------------------------- chat

def fetch_chat(cfg, cookie, limit=None):
    """Project chat, oldest first (the API pages newest-first by timestamp)."""
    out, before = [], None
    while limit is None or len(out) < limit:
        n = 100 if limit is None else min(100, limit - len(out))
        q = f"/project/{cfg['project']}/messages?limit={n}"
        if before:
            q += f"&before={before}"
        page = json.loads(http_get(cfg, cookie, q))
        if not page:
            break
        out.extend(page)
        before = page[-1]["timestamp"]
        if len(page) < n:
            break
    return list(reversed(out))

def cmd_chat(args):
    cfg, cookie = project_context(args.dir)
    msgs = fetch_chat(cfg, cookie, None if args.all else args.limit)
    if args.json:
        print(json.dumps(msgs, indent=2, ensure_ascii=False))
        return
    if not msgs:
        print("(no chat messages)")
    for m in msgs:
        u = m.get("user") or {}
        who = u.get("first_name") or u.get("email") or m.get("user_id")
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["timestamp"] / 1000))
        print(f"[{ts}] {who} <{u.get('email', '?')}>: {m.get('content', '')}")

def _cookie_available(project_dir, cfg):
    try:
        resolve_cookie(project_dir, cfg)
        return True
    except FgError:
        return False

# --------------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="fakegit.py",
        description="Overleaf history -> git (full history, push, labels as "
                    "tags, chat) for Overleaf instances without a git bridge.")
    ap.add_argument("--dir", default=os.getcwd(),
                    help="project directory (default: current directory)")
    ap.add_argument("--verbose", action="store_true", help="more output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--dir", default=argparse.SUPPRESS,
                       help="project directory (default: current directory)")
        p.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                       help="more output")

    p_init = sub.add_parser("init", help="create mirror + working repo, replay history")
    common(p_init)
    p_init.add_argument("--url", help="Overleaf instance base URL")
    p_init.add_argument("--project", help="project id or name (interactive picker if omitted)")
    p_init.add_argument("--cookie", help="session cookie value")
    p_init.add_argument("--cookie-file", help="path to a file with the cookie")
    p_init.add_argument("--max-updates", type=int, default=None,
                        help="replay only the last N Overleaf changes (default: "
                             "the whole history; 0 = snapshot only)")
    p_init.add_argument("--updates-dir", help=argparse.SUPPRESS)
    p_init.add_argument("--from-zip", help=argparse.SUPPRESS)

    p_projects = sub.add_parser("projects",
                                help="list the projects in your account (discovery)")
    common(p_projects)
    p_projects.add_argument("--url", help="Overleaf instance base URL")
    p_projects.add_argument("--search", default=None,
                            help="only show projects whose name matches")
    p_projects.add_argument("--json", action="store_true", help="raw JSON output")
    p_projects.add_argument("--cookie", help=argparse.SUPPRESS)
    p_projects.add_argument("--cookie-file", help=argparse.SUPPRESS)

    p_sync = sub.add_parser("sync", help="replay new changes + merge into your branch")
    common(p_sync)
    p_sync.add_argument("--force", action="store_true",
                        help="snapshot fallback: sync even if content is unchanged")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="report new changes without downloading/merging")
    p_sync.add_argument("--no-stash", action="store_true",
                        help="fail instead of auto-stashing local changes")
    p_sync.add_argument("--backfill", action="store_true",
                        help="also replay old Overleaf history into the mirror "
                             "(rate-limit-paced; merges once caught up)")
    p_sync.add_argument("--cookie", help=argparse.SUPPRESS)
    p_sync.add_argument("--cookie-file", help=argparse.SUPPRESS)
    p_sync.add_argument("--from-zip", help=argparse.SUPPRESS)
    p_sync.add_argument("--updates-dir", help=argparse.SUPPRESS)

    p_watch = sub.add_parser("watch", help="sync periodically (or on edits with --realtime)")
    common(p_watch)
    p_watch.add_argument("--interval", type=int,
                         help="seconds between syncs (default: from config)")
    p_watch.add_argument("--realtime", action="store_true",
                         help="listen on the realtime channel and sync as soon "
                              "as an edit happens (socket.io v1 xhr-polling)")
    p_watch.add_argument("--backfill", action="store_true",
                         help="also replay old Overleaf history (rate-limit-paced)")
    p_watch.add_argument("--force", action="store_true")
    p_watch.add_argument("--cookie", help=argparse.SUPPRESS)
    p_watch.add_argument("--cookie-file", help=argparse.SUPPRESS)
    p_watch.add_argument("--from-zip", help=argparse.SUPPRESS)
    p_watch.add_argument("--updates-dir", help=argparse.SUPPRESS)

    p_serve = sub.add_parser("serve", help="run a read-only git daemon on the mirror")
    common(p_serve)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--listen", default=None,
                         help="bind to a specific address (default: all interfaces)")
    p_serve.add_argument("--background", action="store_true")
    p_serve.add_argument("--stop", action="store_true")

    p_status = sub.add_parser("status", help="show config, mirror, last sync, history")
    common(p_status)

    p_chat = sub.add_parser("chat", help="show the project's chat messages")
    common(p_chat)
    p_chat.add_argument("-n", "--limit", type=int, default=50,
                        help="last N messages (default 50)")
    p_chat.add_argument("--all", action="store_true", help="the whole chat")
    p_chat.add_argument("--json", action="store_true", help="raw JSON (oldest first)")

    # run by the mirror's pre-receive hook on `git push` (not for direct use)
    p_hook = sub.add_parser("pre-receive")
    common(p_hook)

    args = ap.parse_args(argv)
    args.dir = os.path.abspath(args.dir)
    try:
        if args.cmd == "init":
            cmd_init(args)
        elif args.cmd == "sync":
            cmd_sync(args)
        elif args.cmd == "watch":
            cmd_watch(args)
        elif args.cmd == "serve":
            cmd_serve(args)
        elif args.cmd == "projects":
            cmd_projects(args)
        elif args.cmd == "status":
            cmd_status(args)
        elif args.cmd == "chat":
            cmd_chat(args)
        elif args.cmd == "pre-receive":
            cmd_pre_receive(args)
    except FgError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except BrokenPipeError:            # e.g. `fakegit.py status | head`
        try:
            sys.stdout.close()
        except Exception:
            pass
        os._exit(141)

if __name__ == "__main__":
    main()
