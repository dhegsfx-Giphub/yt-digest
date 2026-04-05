# YouTube Insights Digest

A self-hosted server that monitors your favourite YouTube channels, pulls transcripts weekly, and emails you a digest of actionable insights — powered by Claude.

---

## What it does

- Tracks any YouTube channels you add via the web dashboard
- Every week (day/time you choose), fetches new videos and their transcripts
- Sends each transcript to Claude for analysis
- Emails you a clean digest with a summary + 5 actionable insights per video
- Keeps a history of all past digests in the dashboard

---

## Deploy to Railway (free tier, ~5 min)

### 1. Get your API keys

| Key | Where to get it |
|-----|----------------|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API Keys |
| `YOUTUBE_API_KEY` | https://console.cloud.google.com → APIs & Services → YouTube Data API v3 → Credentials |

### 2. Set up email (Gmail recommended)

1. Go to your Google Account → Security → 2-Step Verification → App passwords
2. Create an app password for "Mail"
3. Copy the 16-character password

### 3. Deploy

1. Push this folder to a GitHub repo (public or private)
2. Go to https://railway.app → New Project → Deploy from GitHub repo
3. Select your repo
4. Go to **Variables** and add all the environment variables below
5. That's it — Railway will build and deploy automatically

### 4. Add your channels

Open the Railway-provided URL → **Channels** tab → paste any YouTube @handle or URL.

---

## Environment variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | ✅ | Your Anthropic API key | `sk-ant-...` |
| `YOUTUBE_API_KEY` | ✅ | YouTube Data API v3 key | `AIza...` |
| `DIGEST_EMAIL` | ✅ | Where to send digests | `you@gmail.com` |
| `SMTP_USER` | ✅ | Gmail address to send from | `yourbot@gmail.com` |
| `SMTP_PASS` | ✅ | Gmail app password (not your real password) | `abcd efgh ijkl mnop` |
| `SMTP_HOST` | | SMTP server (default: `smtp.gmail.com`) | `smtp.gmail.com` |
| `SMTP_PORT` | | SMTP port (default: `587`) | `587` |
| `DIGEST_DAY` | | Day to send digest (default: `monday`) | `friday` |
| `DIGEST_HOUR` | | Hour in UTC to send (default: `8`) | `7` |
| `DB_PATH` | | SQLite path (default: `digest.db`) | `/data/digest.db` |

**Tip:** Set `DB_PATH=/data/digest.db` and Railway will persist the database across deploys using the volume configured in `railway.toml`.

---

## Testing

Once deployed, go to the dashboard and click **"Run digest now"** — it will immediately process the last 7 days of videos from your tracked channels and send an email (or you can view the digest in the History tab).

---

## Notes

- Only videos with auto-generated or manual captions will be analysed (most popular videos have them)
- YouTube API free tier allows 10,000 units/day — enough for ~50 channels
- Claude API costs are minimal: roughly $0.01–0.05 per digest depending on video count
