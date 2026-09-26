# abs-storygraph-sync

Listen in [Audiobookshelf](https://www.audiobookshelf.org/), and your [StoryGraph](https://www.thestorygraph.com/) keeps up on its own: books you start show as currently reading, books you finish are marked read, and your progress lands in your reading journal on the days you actually listened.

It's a small Docker container that sits next to ABS. There's no browser automation, just the ABS API and your StoryGraph session cookies. One instance can serve a whole household, each person with their own accounts.

## Features

- **Hands-off sync** of starts, progress and finishes, every 10 minutes or once a day
- **The right edition, every time.** Each book is matched to its StoryGraph audiobook edition by ISBN/ASIN, runtime and narrator, and nothing is written until an edition is confirmed
- **History Import** rebuilds your listening day by day from ABS and backdates it into your StoryGraph journal
- **Real dates**, so a book you started in March doesn't show as started the day you set this up
- **No journal spam.** Progress is only posted when it has actually changed
- **Multi-user**, with local accounts or SSO through any OIDC provider (e.g. [PocketID](https://github.com/pocket-id/pocket-id))

## Setup

### 1. Run it

```sh
git clone --branch feature/edition-aware-matcher https://github.com/Jordiejam/abs-storygraph-sync.git
cd abs-storygraph-sync
docker compose up -d --build
```

Everything is kept under `./data`. To use SSO, uncomment the OIDC and `PUBLIC_URL` lines in [`docker-compose.yml`](docker-compose.yml).

Open **http://your-server:5465**. The first account you create is an admin, who can add more from the **Users** panel; SSO users get an account on first sign-in.

### 2. Get your ABS API token

In Audiobookshelf: **Settings → Users → your user → API Token**.

### 3. Get your StoryGraph cookies

Sign in at [app.thestorygraph.com](https://app.thestorygraph.com), then open DevTools → **Application** → **Cookies** → `app.thestorygraph.com` and copy `_storygraph_session` and `remember_user_token`.

### 4. Fill in Settings

Paste your ABS URL and token and both cookies into **Settings**. The ABS URL depends on how the containers reach each other:

| Setup | Example ABS URL |
|---|---|
| Same Docker network | `http://audiobookshelf:80` |
| Docker Desktop, via ABS's published port | `http://host.docker.internal:13378` |
| ABS elsewhere on your LAN | `http://192.168.1.20:13378` |
| Public or reverse-proxied ABS | `https://abs.example.com` |

Then head to **Editions** to match your books (see below).

## Choosing what syncs

**Sync scope** picks which books are sent:

- **In Progress** (default): what you're listening to now
- **+ Finished**: plus books you've completed, marked read
- **Entire Library**: plus everything unstarted, marked to-read

**Sync frequency** picks how progress is sent:

- **Every N minutes** (default): progress goes up once you've listened another `SYNC_THRESHOLD_MINUTES`
- **Daily**: at a time you choose, yesterday's listening goes into your journal as its own dated entry. Missed days are caught up after downtime, but it never reaches back further than your first daily run.

Starts and finishes sync at the next poll in either mode. When a book finishes, its last days are written first, then it's marked read, because StoryGraph won't take dated entries for a book it already has as read.

## Editions

Sync only ever writes to an edition you've confirmed, so your progress never ends up on the paperback or someone else's narration.

On the **Editions** page, tick books and hit **Search selected**. Each search comes back with either:

- a **suggestion** with the reason it matched (same ISBN/ASIN, or a runtime within a few minutes), ready to confirm; or
- a list of audio editions to choose from. You can also search with your own words, or paste a StoryGraph book URL.

When several editions fit, the one you've already read on StoryGraph wins, then the one whose narrator and publisher match ABS.

**Auto-confirm** (on by default) searches for each book as you start it and confirms strong matches itself: the ABS tag, an exact ISBN/ASIN, or a runtime match with the right narrator and no close rivals. These wait under **To review**. Nothing is written to them until the next poll, and you can **Keep** or **Undo** them. History Import won't write to one until you keep it.

**ABS tags.** Confirming an edition tags the book `storygraph:<book id>` in Audiobookshelf, so the choice survives a lost `./data` and shows up for everyone on the server. **Sync ABS tags** lines up tags and confirmations across your library. Tagging needs an ABS user allowed to edit books.

## History Import

Open **History** on any book to see your listening rebuilt from ABS sessions, one row per day. Rewinds and suspicious jumps are flagged. Tick the days you want and import them as backdated journal entries. Days already on StoryGraph are skipped, and a finished book is marked read, dated across those days.

Importing into a book StoryGraph already has as read adds the days as a reread, so it asks first.

## Configuration

These environment variables apply to the whole instance. Everything else is per user, in Settings.

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5465` | Web UI port |
| `POLL_INTERVAL` | `600` | Seconds between checks of ABS |
| `SYNC_THRESHOLD_MINUTES` | `5` | New listening needed before a frequent-mode progress update |
| `OIDC_ISSUER` | *(unset)* | OIDC provider base URL (serving `/.well-known/openid-configuration`) |
| `OIDC_CLIENT_ID` | *(unset)* | OIDC client ID |
| `OIDC_CLIENT_SECRET` | *(unset)* | OIDC client secret |
| `PUBLIC_URL` | *(unset)* | External base URL, e.g. `https://abs-sync.example.com`, if the OIDC redirect comes out wrong |

**OIDC redirect URI:** `https://your-domain.example.com/auth/callback`. Behind an HTTPS reverse proxy, forward `X-Forwarded-Proto` and `X-Forwarded-Host` (Caddy does this by default) so the redirect uses `https://`, or set `PUBLIC_URL`.

## Development

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

Source is bind-mounted and reloads on change, so a `git pull` is enough; only rebuild after dependency or Dockerfile changes. Dev mode is read-only: no background sync, and Sync Now is disabled.

## Good to know

- **Cookies expire.** When syncing stops, copy fresh cookies from your browser (step 3) into Settings.
- **It's unofficial.** StoryGraph has no public API, so this makes the same requests the website does. A change on their side can break it without warning.
- **Upgrading from an earlier version?** Editions you picked by hand stay confirmed. Automatic ones come back as suggestions and need one click to confirm before sync uses them again.

## Credits

Inspired by [KOreader-storygraph](https://github.com/AsmaraLehrmann/KOreader-storygraph) and [storygraph.koplugin](https://github.com/burneracc0112/storygraph.koplugin).
