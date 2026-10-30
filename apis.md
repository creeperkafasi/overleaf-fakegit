# Hidden Overleaf APIs (overleaf.ktu.edu.tr)

Reverse-engineered from live probing + the open-source ShareLaTeX web source
(`research/src-web/`). Instance: `https://overleaf.ktu.edu.tr` (old ShareLaTeX,
Socket.IO v1 era, nginx 1.14.2). All endpoints require the session cookie
unless noted. Sample project used: `6993449de0f8759721a4daa7` ("868 Mhz - Review",
1035+ updates, versions v0..v6352).

## Auth

- Session cookie: `overleaf.sid` — a signed Express cookie, value `s:<sig>`.
  Both raw (`overleaf.sid=s:...`) and URL-encoded (`overleaf.sid=s%3A...%2B...`)
  forms work.
- Get it from DevTools → Network → any request to the instance → Request Headers.
- Per-user identity: dashboard HTML exposes `ol-usersEmail` and `ol-user_id` metas.

## Endpoints

### 1. Full project zip (latest) — NOT rate-limited
```
GET /project/{id}/download/zip
```
- Returns the current project as a zip (root = project files, no wrapping dir).
- Includes PDFs/SVGs (e.g. ~2 MB compressed here).
- Not throttled (the versioned endpoint below is). ~1-15 s server-side (the
  server builds the zip on demand; slow days 15-35 s).
- Referer header: `https://overleaf.ktu.edu.tr/project/{id}`.

### 2. Change list / updates — live, cheap
```
GET /project/{id}/updates?min_count=N[&before=<epoch_ms>]
```
- JSON: `{"updates":[...], "nextBeforeTimestamp": <int|null>}`.
- One update:
  ```json
  {"fromV": 6351, "toV": 6352,
   "meta": {"users": [{"first_name":"457519","last_name":"","email":"457519@ogr.ktu.edu.tr","id":"6999ac42..."}],
            "start_ts": 1787384685039, "end_ts": 1787384685039},
   "labels": [], "pathnames": ["cas-dc-template.tex"], "project_ops": []}
  ```
- `fromV`/`toV`: contiguous per-change version range; newest-first; `toV` of
  update N == `fromV` of update N+1. `end_ts` is epoch **milliseconds**.
- `min_count` is a minimum; the server may return fewer (here: 103 for 463
  versions). Paginate backwards with `before=nextBeforeTimestamp`.
- `project_ops` are almost always empty; occasionally a file-add marker:
  `{"add": {"pathname": "x.pdf"}, "atV": 6103}` — no content, no text ops.
- This is the authoritative, current history (the `/latest/history` one below
  is only the latest chunk — §2b).

### 2b. History API — per-version content, NOT rate-limited
The live instance runs Overleaf's modern history service (newer than
`research/src-web`). Verified: replaying all 1230 updates of the 868 project
through these gives a tip byte-identical to `/download/zip`.
```
GET /project/{id}/filetree/diff?from=V&to=V         files at V: {"diff":[{pathname, editable, operation?}]}
GET /project/{id}/diff?pathname=P&from=A&to=B        text diff: {"diff":[{"u"|"i"|"d": str, meta?}]}
GET /project/{id}/changes?since=N[&paginated=true]   raw change log, one entry per version
GET /project/{id}/blob/{hash}                        file bytes by hash (= git blob SHA-1)
GET /project/{id}/latest/history[?version=V]         the history chunk containing V
```
- `diff` with `from=to=V` returns one `{"u": <full text>}` segment = the file
  at V; for binaries `{"diff": {"binary": true}}`. `meta.users` on `i`/`d`
  segments says who typed what.
- `filetree/diff` `operation`: added / removed (with `deletedAtV`) /
  renamed / absent (= unchanged or edited).
- `changes`: entry k turns version k into k+1. Operations: text edits
  `{pathname, textOperation:[retain int | insert str | -delete int]}`; adds
  `{pathname, file:{hash, stringLength|byteLength}}`; removes
  `{pathname, newPathname:""}`; renames `{pathname, newPathname}`.
  Non-paginated `since=0` returns everything (7287 entries here, ~3 MB);
  `paginated=true` → `{changes:[≤2000], hasMore}`.
- `latest/history` is the *latest chunk* (here `startVersion` 6000) — it
  looked "stale" earlier only because older versions live in older chunks.
- `/updates` `project_ops`: `{"add":{pathname}}`, `{"remove":{pathname}}`,
  `{"rename":{pathname,newPathname}}`, each with `atV` (op at k → version k+1).
- Labels: `GET /project/{id}/labels` → `[{id, comment, version, user_id,
  created_at, user_display_name}]`; `POST` (CSRF) `{comment, version}`;
  `DELETE /project/{id}/labels/{label_id}`.

### 2c. Chat
```
GET  /project/{id}/messages?limit=N[&before=<ts ms>]   newest first
POST /project/{id}/messages {content}                  (CSRF; not used)
```
Items: `{id, content, timestamp (ms), user_id, user{id, first_name, email}}`.
Review comments / track changes (`/threads`, `/ranges`) → 404 here.

### 3. Versioned project zip — RATE-LIMITED (30/hour/user)
```
GET /project/{id}/version/{v}/zip
```
- Exact full-project snapshot at version `v` (cumulative). `v=0` → empty zip
  (22 bytes). Works for arbitrary `v` (e.g. `/version/6352/zip`).
- **Rate limit** (from source, `RateLimiterMiddleware.rateLimit({
  endpointName:'download-project-revision', maxRequests:30, timeInterval:60*60})`):
  30 requests per rolling hour, keyed **per user** (session), shared across all
  projects and with the browser's History tab. Exceed → `429` with body
  `Rate limit reached, please try again later`. No Retry-After header; ~1 h window.

### 4. Dashboard / project list (discovery)
```
GET /project
```
- HTML shell; the project list is embedded in an escaped-JSON meta:
  ```html
  <meta name="ol-prefetchedProjectsBlob" data-type="json"
        content="{&quot;totalSize&quot;:4,&quot;projects&quot;:[...]}">
  ```
- `projects[]` items: `id`, `name`, `archived`, `trashed`, `accessLevel`
  (owner/readWrite/readOnly), `source` (invite/owner), `lastUpdated` (ISO),
  `lastUpdatedBy{id,email,firstName,lastName}`, `owner{id,email,firstName}`,
  plus collaborators.
- Unescape (`html.unescape`) then `json.loads` the content attribute.
- Other useful metas on the same page: `ol-csrfToken`, `ol-ExposedSettings`
  (appName, adminEmail), `ol-userEmails`, `ol-user`, `ol-navbar`, `ol-i18n`.

### 5. History JSON (latest chunk) — early notes, superseded by §2b
```
GET /project/{id}/latest/history
```
- `{"chunk":{"history":{"snapshot":{...},"changes":[...]}}}`.
- `snapshot`: `files` = `{path: {hash: <40-hex>, stringLength | byteLength}}`,
  `projectVersion` (float-ish, e.g. `"116.1"` — NOT the updates `v` numbers),
  `timestamp`.
- `changes`: grouped edits (352 here vs 1035 raw updates), each
  `{authors, operations, timestamp, v2Authors, v2DocVersions:{docId:{pathname,v}}}`.
  Operations: `{"pathname": "...", "textOperation": [<retain>, "<insert>",
  <retain>, -<delete>, ...]}` — the ShareLaTeX OT format (positive ints =
  retain, negative = delete, strings = insert). **286/347 contain deletes, so
  they're NOT invertible** without the deleted text.
- Caveats: snapshot was 3 days stale on this instance (Aug 19 vs Aug 22 live
  edits); version numbers don't correlate with `/updates`; file-level ops are
  sparse (only 5 non-text ops; only 2 files ever appear in text ops).

### 6. Socket.IO v1 realtime (xhr-polling) — live edits + project state
```
GET  /socket.io/1/?projectId={id}&t=<epoch_ms>                       # handshake
POST /socket.io/1/xhr-polling/{sid}?projectId={id}&t=<epoch_ms>      # open
GET  /socket.io/1/xhr-polling/{sid}?projectId={id}&t=<epoch_ms>      # poll
```
- Handshake → `sid:60:60:websocket,xhr-polling`. **`projectId` MUST be in the
  handshake query** — without it the session connects but every poll returns
  `7:::1+0` (error frame, reason "client not handshaken"-style).
- Open POST (empty body) → `1::`. First poll → `5:::{"name":"joinProjectResponse","args":[{...}]}`
  with the full project JSON: `publicId`, `project{_id, name, rootDoc_id,
  rootFolder[{_id,name,files:[{_id,name,hash,created}]}], docs:[{_id,name}],
  owner, members:[{_id,first_name,email,privileges}], features{...},
  trackChangesState}`, `permissionsLevel`, `protocolVersion:2`.
- The `hash` fields are docstore blob hashes (matching the `/latest/history`
  snapshot hashes for unchanged files).
- Subsequent polls long-poll for events: `5:::{"name":"otUpdateApplied",...}`
  (edits), `clientTracking.clientUpdated/clientDisconnected` (presence),
  `2::` heartbeats. Multi-packet payloads are `\ufffd<len>\ufffd<packet>`
  concatenated.
- Websocket transport (`/socket.io/1/websocket/{sid}?projectId=...`) is
  **broken**: nginx returns 502 on the upgrade. Use xhr-polling only.
- `loadDocVersions` (old ShareLaTeX history-over-realtime) is **not handled**
  on this instance — per-version content is zips-only.

### 7. Write API (used by `git push`) — CSRF-protected
All need header `X-Csrf-Token: <token>` — the `ol-csrfToken` meta on the
editor page `GET /project/{id}` (session-bound). Entity ids come from the
realtime `joinProjectResponse` (`rootFolder[0]` = root; `folders`, `docs`,
`fileRefs` each `{_id, name}`).
```
POST   /Project/{id}/upload?folder_id={folder}   multipart: `name` field + `qqfile` file part
POST   /project/{id}/folder                      JSON {name, parent_folder_id} -> {_id, name, ...}
DELETE /project/{id}/{doc|file|folder}/{entity_id}
POST   /project/{id}/{type}/{entity_id}/rename   {name}          (not used yet)
POST   /project/{id}/{type}/{entity_id}/move     {folder_id}     (not used yet)
```
- Upload is an **upsert**: same name in the same folder replaces content in
  place (doc id + history kept); text → doc, binary → file. Response
  `{"success":true,"entity_id":...,"entity_type":"doc"|"file"}`.
- The live instance is newer than `research/src-web`: it reads the file name
  from the `name` form field (without it: 422 `invalid_filename`).
- Rate limits (source): upload 200 / 15 min / project; add-doc 30/min;
  add-folder 60/min.

### 8. Misc / dead ends
- `GET /project/{id}/git` → 404 (no git bridge).
- `GET /project/{id}/version/{v}/{pathname}` → 404 (no per-file-at-version
  route; use `/diff` or `/blob`).
- `GET /project/{id}/doc/{doc_id}/version/{version_id}/restore` — exists in
  source (POST restore from a doc version); not exercised.
- `GET /Project/{id}/file/{File_id}` — file download by internal file id
  (exists in source; not exercised).

## Gotchas

- **Version spaces line up**: `/updates` `fromV/toV`, `/changes` indices and
  history-chunk versions (`startVersion` + index) are the same numbering.
  (`projectVersion` strings like "116.1" inside changes are something else.)
- **Per-version content without the zip limit**: text via `/diff`, binaries
  via `/changes` hashes + `/blob` (§2b). Only `/version/{v}/zip` is throttled.
- Timestamps: `updates[].meta.end_ts` = epoch ms (UTC); the dashboard's
  `lastUpdated` = ISO.
- The instance is a shared university account: `ol-prefetchedProjectsBlob`
  lists every project the account can see, with `accessLevel` per project.
