# Otter Nonsense Hytale — Discord status bot

Polls the Nitrado Query endpoint and brings the server to life in Discord.

## Features

| Feature | Channel needed | Bot perms |
|---|---|---|
| Pinned live status embed | text | View Channels, Send Messages, Embed Links, Read Message History, Pin Messages |
| Bot presence (`Watching 4/20 on Otter Nonsense`) | — | — |
| `/status`, `/players`, `/serverinfo`, `/profile`, `/stats` | — | (uses the `applications.commands` scope, no perm needed) |
| Join / leave announcements | text | View Channels, Send Messages, Embed Links |
| Voice-channel live counter | voice | **Manage Channel — set as a per-channel override only, not at the role level** |
| Daily 9am recap | text | View Channels, Send Messages, Embed Links |

Each feature is independently toggled by setting (or omitting) its channel ID env var.

## What it tracks

A small SQLite database stores:
- `players` — every UUID we've seen, plus first/last seen.
- `sessions` — start/end of each player's time on the server.
- `snapshots` — minute-by-minute player count (for peak charts).

Everything is derived from polling the Query endpoint — no in-game cooperation required.

## Setup

### 1. Create the Discord application

1. <https://discord.com/developers/applications> → **New Application** → name it.
2. **Bot** tab → **Reset Token** → copy. That's `DISCORD_TOKEN`.
3. **Bot** tab → disable **Public Bot** unless you want anyone able to add it.
4. **OAuth2 → URL Generator**:
   - **Scopes** (these appear at the top of the list — scroll up if you don't see them):
     - `bot`
     - `applications.commands`
   - **Bot permissions — General:**
     - View Channels
   - **Bot permissions — Text:**
     - Send Messages
     - Embed Links
     - Read Message History
     - Pin Messages
   - **Do NOT** check `Manage Channels` here. We grant that as a per-channel override on just the live-counter voice channel after the bot is in the server.
5. Open the generated URL and invite the bot to your server.
6. **Voice-counter permission (only if using that feature):**
   - Right-click your live-counter voice channel → Edit Channel → Permissions
   - Click `+` and add either the bot's role OR the bot user itself
   - Grant only **Manage Channel** here (and View Channel + Connect if not inherited).

### 2. Get IDs

Enable Discord Developer Mode (Settings → Advanced). Then right-click and "Copy ID" on:
- The status text channel → `STATUS_CHANNEL_ID`
- The activity text channel → `ACTIVITY_CHANNEL_ID` (optional)
- The voice channel for live counter → `COUNTER_VOICE_CHANNEL_ID` (optional)
- The recap text channel → `RECAP_CHANNEL_ID` (optional)
- Your server icon → `GUILD_ID` (recommended — slash commands appear instantly when set)

### 3. Configure

```bash
cp .env.example .env
# edit .env
```

### 4. Run

#### Locally

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt
python bot.py
```

#### Docker

```bash
docker build -t otter-bot .
docker run -d --name otter-bot --env-file .env -v otter_data:/data otter-bot
```

#### Fly.io (recommended)

```bash
fly auth login
fly launch --no-deploy --copy-config
fly volumes create otter_bot_data --size 1 --region ord
fly secrets set \
  DISCORD_TOKEN=... \
  STATUS_CHANNEL_ID=... \
  GUILD_ID=... \
  QUERY_USER=serviceaccount.discord \
  QUERY_PASS=... \
  ACTIVITY_CHANNEL_ID=... \
  COUNTER_VOICE_CHANNEL_ID=... \
  RECAP_CHANNEL_ID=...
fly deploy
fly logs
```

## Notes

- `INSECURE_TLS=true` is required because the WebServer plugin defaults to a self-signed cert.
  Switch to `false` once you put a real cert (Let's Encrypt or PEM) on the WebServer plugin.
- Voice-channel renames are rate-limited by Discord (2 per 10 min per channel). The bot
  enforces a 5-minute minimum interval and skips no-op renames.
- The bot does not see anything that happened before it started watching. History grows
  from the moment the bot first connects.
- `state.json` keeps the pinned status message ID. If lost, the bot posts a fresh
  message and re-pins it; previous status messages can be deleted manually.
