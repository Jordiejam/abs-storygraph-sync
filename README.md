# abs-storygraph-sync

Automatically syncs your [Audiobookshelf](https://www.audiobookshelf.org/) listening progress to [StoryGraph](https://www.thestorygraph.com/).

Runs as a lightweight Docker container alongside ABS. No browser automation — uses the ABS REST API and StoryGraph session cookies directly. Supports multiple people sharing one instance, each with their own ABS/StoryGraph credentials.

## Features

- Multi-user: everyone gets their own account, ABS/StoryGraph credentials, and sync state
- Login via local username/password, or SSO through any OIDC provider (e.g. [PocketID](https://github.com/pocket-id/pocket-id))
- Configurable auto-sync frequency: every `POLL_INTERVAL` (10 minutes by default), or a daily history reconciliation at a chosen local time
- Book starts and finishes sync promptly even when ordinary progress is set to daily
- Configurable sync scope: just in-progress books, in-progress + finished, or your entire library
- Web UI to manage credentials, view logs, and trigger a manual sync
- Progress is only pushed to StoryGraph when it actually changes (no duplicate journal entries)
- An **Editions** page where you match each book to its StoryGraph edition: tick books to search, check the suggestion (ISBN/ASIN match or closest runtime), and confirm it. Sync only ever writes to a confirmed edition, and can confirm strong matches for books you start listening to by itself
- Daily history reconstructed from Audiobookshelf playback sessions, shown day by day for review before an opt-in History Import that backdates StoryGraph journal entries
- Accounts, settings, and sync state persist across restarts

## Setup

### 1. Run with Docker Compose

Clone this fork and check out the edition-aware matcher branch:

```sh
git clone --branch feature/edition-aware-matcher https://github.com/Jordiejam/abs-storygraph-sync.git
cd abs-storygraph-sync
```

The included [`docker-compose.yml`](docker-compose.yml) builds the image and persists
everything under `./data`; uncomment its OIDC and `PUBLIC_URL` lines if you need them.

```sh
docker compose up -d --build
```

Open **http://your-server:5465** — the first visit prompts you to create an account, which becomes an admin. Admins can add more local accounts from the **Users** panel; anyone who signs in via SSO gets an account automatically on first login.

The ABS URL depends on how the two services can reach each other:

| Setup | Example ABS URL |
|---|---|
| Same Docker network | `http://audiobookshelf:80` |
| Docker Desktop, using ABS's published port | `http://host.docker.internal:13378` |
| ABS elsewhere on your LAN | `http://192.168.1.20:13378` |
| Public/reverse-proxied ABS | `https://abs.example.com` |

Use the actual Audiobookshelf service name, internal port, host address, and
published port from your deployment. Normal bridge networking and a published
web port are used so the sync service works across Linux and Docker Desktop.

### Development mode

For local development, layer the development override onto the normal Compose
file:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

Python files, templates and static assets are bind-mounted into the container and Flask
reloads them when they change, so routine source changes only need a `git pull`,
not an image rebuild. Development mode is explicitly read-only: the background
poller is stopped and the manual sync endpoint is blocked. Rebuild only after a
dependency or Dockerfile change.

### 2. Get your ABS API token

In Audiobookshelf: **Settings → Users → your user → API Token**

### 3. Get your StoryGraph session cookies

1. Log in to [app.thestorygraph.com](https://app.thestorygraph.com) in your browser
2. Open DevTools → **Application** → **Cookies** → `app.thestorygraph.com`
3. Copy the values for:
   - `_storygraph_session`
   - `remember_user_token`

### 4. Enter your credentials

Paste your ABS URL/token and StoryGraph cookies into the **Settings** card in the web UI (each account has its own). You can also choose the sync scope and frequency there.

## Configuration

Instance-wide environment variables (set once by whoever deploys the container):

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5465` | Port for the web UI |
| `POLL_INTERVAL` | `600` | How often to check for progress and book start/finish transitions (seconds) |
| `SYNC_THRESHOLD_MINUTES` | `5` | Minimum new minutes listened before triggering a sync in frequent mode |
| `OIDC_ISSUER` | *(unset)* | Base URL of your OIDC provider (must expose `/.well-known/openid-configuration`) |
| `OIDC_CLIENT_ID` | *(unset)* | OIDC client ID |
| `OIDC_CLIENT_SECRET` | *(unset)* | OIDC client secret |
| `PUBLIC_URL` | *(unset)* | Externally-visible base URL, e.g. `https://abs-sync.example.com` (no trailing slash). Only needed if auto-detection below doesn't work for your setup |

Everything else — ABS credentials, StoryGraph cookies, sync scope, and sync frequency — is per-user, set through the web UI, no restart needed.

### OIDC redirect URI

When registering this app with your OIDC/OAuth provider (Google, PocketID, etc.), the redirect URI to configure is:

```
https://your-domain.example.com/auth/callback
```

The app is behind a plain HTTP `python app.py` process, so if you're reverse-proxying it over HTTPS (Caddy, nginx, Traefik, ...), it needs to know the request actually arrived over HTTPS in order to generate that redirect URI correctly when it talks to the provider — otherwise it'll send an `http://` redirect URI even though your proxy terminates TLS, and the provider will reject it as a mismatch. This is handled automatically as long as your reverse proxy forwards the standard `X-Forwarded-Proto`/`X-Forwarded-Host` headers (Caddy's `reverse_proxy` does this by default; other proxies may need it configured explicitly). If auto-detection still isn't giving the right URL for your setup, set `PUBLIC_URL` to force it.

## Sync scope

Each user picks how much of their library to sync, in **Settings**:

- **In Progress** (default) — only books you're currently listening to
- **+ Finished** — the above, plus books you've completed (marked "read" on StoryGraph)
- **Entire Library** — every book, including ones you haven't started (marked "to-read" on StoryGraph)

## Sync frequency

Each user can choose:

- **Every N Minutes** (default, N from `POLL_INTERVAL`) — progress is pushed after at least `SYNC_THRESHOLD_MINUTES` of additional listening
- **Daily** — at the selected local time (midnight by default), completed Audiobookshelf listening days are reconciled to dated StoryGraph progress entries

The app still checks Audiobookshelf every `POLL_INTERVAL` seconds in Daily mode, but only contacts StoryGraph when a book starts, a book finishes, the daily run is due, or the user selects **Sync Now**. At midnight, it reconciles the previous day's ABS checkpoint using the same duplicate-safe, verified write path as History Import, so the entry keeps the day the listening happened. After downtime it catches up missed completed days; the first daily run only considers yesterday and never sweeps older history automatically. The daily run only covers books you're still reading. A book that finishes has its remaining days, including the finish day, written straight away, and is then marked read, since StoryGraph can't take a dated entry for a book it already has as read. The first check after enabling this version quietly records existing books so old starts and finishes are not replayed.

## Editions

Sync and History Import only write to a confirmed StoryGraph edition, so
nothing lands on the wrong edition by guesswork. The **Editions** page lists your
whole ABS library, whatever your sync scope:

- Tick books and select **Search selected**. They are looked up one at a time.
- With **Auto-confirm Editions** on in Settings (the default), auto-sync also
  looks up each book you start listening to, once, and a few per poll at most.
  It confirms the edition itself only on a strong match: the ABS tag, an exact
  ISBN/ASIN, or a runtime match whose narrator agrees with ABS and that no other
  edition comes close to (unless the narrator or your earlier read sets it
  apart). A narrator or language that disagrees rules it out. Anything weaker
  stays a suggestion for you to confirm.
  - Nothing is written to an auto-confirmed edition until the next poll, so you
    have one polling interval to catch a wrong pick. Auto-confirmed books appear
    under **To review** with **Keep** and **Undo**. Undo turns the pick back into
    a suggestion and the book is never auto-confirmed again.
  - An auto-confirmed edition isn't tagged in ABS, and History Import won't write
    to it, until you keep it.
- Lookups read StoryGraph's audio-only edition list (up to three pages, in the
  book's language when ABS knows it) as well as the first page of all editions.
- A confident match (exact ISBN/ASIN, or a runtime within a conservative tolerance)
  appears as a **suggestion** to confirm, with the reason it matched. When several
  editions qualify, the one you've already read on StoryGraph comes first, then
  the ABS narrator and publisher. Otherwise you can choose from the audio editions
  found, search again with your own words, or paste a StoryGraph book URL.
- Confirming an edition also tags the book in Audiobookshelf with
  `storygraph:<StoryGraph book id>`, replacing any older `storygraph:` tag. The
  next lookup for that book, from any account or after losing this app's data,
  suggests the tagged edition ahead of everything else. You can also add the tag
  by hand in ABS. Writing it needs an ABS user allowed to update books; without
  that permission the edition is still confirmed, just not tagged.
- **Sync ABS tags** lines the two up across the whole library: books tagged in
  ABS but not confirmed here (or only auto-confirmed) are confirmed as the
  tagged edition, and books you confirmed without a tag get one. A book confirmed as a different edition from its
  tag is left alone and flagged on its row, so you can pick which to keep. It
  only talks to ABS, never StoryGraph.
- A book without a confirmed edition is skipped by sync and reported as
  "needs edition". It syncs normally once you confirm one.

Upgrading from an earlier version: editions picked by hand in History Import stay
confirmed. Editions that sync or History Import chose automatically show up as
suggestions and need one click to confirm before sync writes to them again.

## Dates on StoryGraph

When a book is set to currently reading or marked read, StoryGraph dates it by
the day that happens, or leaves it undated. The app then moves those dates to when
Audiobookshelf says you started and finished, so a late sync or a History
Import doesn't show a book as started on the day it was imported.

History Import writes a book's days while it's currently reading, and marks a
finished book read only once they're all in: StoryGraph files a dated entry
under the read in progress, and treats one on a book it already has as read as
the start of a reread. Importing into a book already marked read asks first,
and adds the days as a reread if you go ahead.

## How it works

1. Every `POLL_INTERVAL` seconds, fetches one lightweight ABS progress snapshot per user
2. Starts and finishes sync promptly; frequent mode pushes changed progress, while daily mode reconciles completed listening days from ABS history
3. Each book is written to the StoryGraph edition confirmed for it on the **Editions** page (stored against the stable Audiobookshelf item ID); books without one are skipped
4. Progress/status is only pushed if it actually changed since the last successful sync, preventing duplicate reading journal entries

StoryGraph has no public API — this tool uses session cookies to make the same requests the website does.

> **⚠️ Fragility warning:** Because this tool reverse-engineers StoryGraph's internal, unpublished web endpoints, it is inherently brittle. Any change StoryGraph makes to their HTML structure, URL routes, CSRF handling, or cookie behaviour can break the sync without warning and with no recourse. There is no official API to fall back on.

## Session cookie expiry

StoryGraph session cookies expire periodically. When the sync stops working, grab fresh cookies from your browser (step 2 above) and paste them into the **Settings** tab of the web UI.

## Credits

Inspired by [KOreader-storygraph](https://github.com/AsmaraLehrmann/KOreader-storygraph) and [storygraph.koplugin](https://github.com/burneracc0112/storygraph.koplugin).
