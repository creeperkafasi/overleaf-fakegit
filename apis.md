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
  is stale).

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

### 5. History JSON — stale + grouped + different version space
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

### 7. Misc / dead ends
- `GET /project/{id}/git` → 404 (no git bridge).
- `GET /project/{id}/blob/{hash}` → 404 (no blob-by-hash endpoint).
- `GET /project/{id}/doc/{doc_id}/version/{version_id}/restore` — exists in
  source (POST restore from a doc version); not exercised.
- `GET /Project/{id}/file/{File_id}` — file download by internal file id
  (exists in source; not exercised).

## Gotchas

- **Version spaces differ**: `/updates` uses `fromV/toV` (int, project-global);
  `/latest/history` uses `projectVersion` (float) + per-doc `v` counters.
- **The zip endpoint is the only per-version content source** on this instance
  (no loadDocVersions, empty project_ops, stale history JSON) — and it's
  throttled to 30/hour/user.
- Timestamps: `updates[].meta.end_ts` = epoch ms (UTC); the dashboard's
  `lastUpdated` = ISO.
- The instance is a shared university account: `ol-prefetchedProjectsBlob`
  lists every project the account can see, with `accessLevel` per project.
