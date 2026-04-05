import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import anthropic
from youtube_transcript_api import YouTubeTranscriptApi
from googleapiclient.discovery import build
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import sqlite3
import pytz

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
YOUTUBE_API_KEY   = os.environ.get("YOUTUBE_API_KEY", "")
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER         = os.environ.get("SMTP_USER", "")
SMTP_PASS         = os.environ.get("SMTP_PASS", "")
DIGEST_EMAIL      = os.environ.get("DIGEST_EMAIL", "")
DIGEST_DAY        = os.environ.get("DIGEST_DAY", "monday")   # day of week
DIGEST_HOUR       = int(os.environ.get("DIGEST_HOUR", "8"))  # hour (UTC)
DB_PATH           = os.environ.get("DB_PATH", "digest.db")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  TEXT UNIQUE NOT NULL,
                name        TEXT,
                added_at    TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS seen_videos (
                video_id    TEXT PRIMARY KEY,
                channel_id  TEXT,
                title       TEXT,
                published   TEXT,
                processed   INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS digests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  TEXT DEFAULT (datetime('now')),
                video_count INTEGER,
                html        TEXT
            );
        """)

init_db()

# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------

def resolve_channel_id(raw: str) -> tuple[str, str]:
    """Accept channel URL, @handle, or bare ID. Returns (channel_id, name)."""
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

    # Strip URL noise
    for prefix in ["https://www.youtube.com/", "https://youtube.com/", "http://www.youtube.com/"]:
        raw = raw.replace(prefix, "")

    if raw.startswith("@"):
        handle = raw[1:]
    elif raw.startswith("channel/"):
        cid = raw.replace("channel/", "").split("/")[0]
        resp = youtube.channels().list(part="snippet", id=cid).execute()
        items = resp.get("items", [])
        if not items:
            raise ValueError(f"Channel not found: {cid}")
        return cid, items[0]["snippet"]["title"]
    elif raw.startswith("@") is False and "/" not in raw and len(raw) == 24 and raw.startswith("UC"):
        cid = raw
        resp = youtube.channels().list(part="snippet", id=cid).execute()
        items = resp.get("items", [])
        name = items[0]["snippet"]["title"] if items else cid
        return cid, name
    else:
        handle = raw.lstrip("@").split("/")[0]

    resp = youtube.channels().list(part="snippet", forHandle=handle).execute()
    items = resp.get("items", [])
    if not items:
        raise ValueError(f"Could not resolve channel: @{handle}")
    return items[0]["id"], items[0]["snippet"]["title"]


def fetch_recent_videos(channel_id: str, days: int = 7) -> list[dict]:
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    published_after = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = youtube.search().list(
        part="snippet",
        channelId=channel_id,
        publishedAfter=published_after,
        type="video",
        maxResults=10,
        order="date"
    ).execute()
    return [
        {
            "video_id":  item["id"]["videoId"],
            "title":     item["snippet"]["title"],
            "published": item["snippet"]["publishedAt"],
            "channel_id": channel_id,
        }
        for item in resp.get("items", [])
    ]


def fetch_transcript(video_id: str) -> str | None:
    try:
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id)
        return " ".join(s.text for s in transcript)
    except Exception as e:
        log.warning(f"No transcript for {video_id}: {e}")
        return None

# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

def analyse_video(title: str, transcript: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are extracting actionable insights from a YouTube video transcript.

Video title: "{title}"

Transcript:
{transcript[:14000]}

Return JSON only (no markdown, no backticks):
{{
  "summary": "2-3 sentence summary of what this video is actually about",
  "actions": [
    "Specific action the viewer can take, starting with a verb",
    "Another specific action",
    "Another specific action",
    "Another specific action",
    "Another specific action"
  ]
}}

Actions must be concrete and specific to this video's content — not generic advice."""

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    text = msg.content[0].text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)

# ---------------------------------------------------------------------------
# Digest builder
# ---------------------------------------------------------------------------

def build_digest_html(results: list[dict]) -> str:
    rows = ""
    for r in results:
        actions_html = "".join(
            f'<li style="margin-bottom:8px;font-size:14px;line-height:1.5;">'
            f'<span style="display:inline-block;width:20px;height:20px;border-radius:50%;'
            f'background:#e8f4fd;color:#1a6fa8;font-size:11px;font-weight:600;'
            f'text-align:center;line-height:20px;margin-right:8px;">{i+1}</span>'
            f'{a}</li>'
            for i, a in enumerate(r["analysis"]["actions"])
        )
        rows += f"""
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;
                    margin-bottom:24px;overflow:hidden;">
          <div style="background:#f9fafb;padding:14px 20px;border-bottom:1px solid #e5e7eb;">
            <div style="font-size:11px;color:#6b7280;text-transform:uppercase;
                        letter-spacing:0.06em;margin-bottom:4px;">{r['channel_name']}</div>
            <div style="font-size:15px;font-weight:600;color:#111827;">
              <a href="https://youtube.com/watch?v={r['video_id']}"
                 style="color:#111827;text-decoration:none;">{r['title']}</a>
            </div>
          </div>
          <div style="padding:16px 20px;">
            <div style="font-size:11px;font-weight:600;text-transform:uppercase;
                        letter-spacing:0.06em;color:#6b7280;margin-bottom:6px;">Summary</div>
            <div style="font-size:14px;color:#374151;line-height:1.6;margin-bottom:14px;">
              {r['analysis']['summary']}
            </div>
            <div style="font-size:11px;font-weight:600;text-transform:uppercase;
                        letter-spacing:0.06em;color:#6b7280;margin-bottom:8px;">Actionable insights</div>
            <ul style="list-style:none;padding:0;margin:0;">{actions_html}</ul>
          </div>
        </div>"""

    date_str = datetime.utcnow().strftime("%d %B %Y")
    return f"""
<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:620px;margin:32px auto;padding:0 16px;">
    <div style="margin-bottom:24px;">
      <div style="font-size:22px;font-weight:700;color:#111827;">Your weekly digest</div>
      <div style="font-size:13px;color:#6b7280;margin-top:4px;">{date_str} · {len(results)} video{'s' if len(results)!=1 else ''}</div>
    </div>
    {rows}
    <div style="text-align:center;font-size:12px;color:#9ca3af;margin-top:16px;padding-bottom:32px;">
      Sent by your YouTube Insights Digest server
    </div>
  </div>
</body></html>"""

# ---------------------------------------------------------------------------
# Email sender
# ---------------------------------------------------------------------------

def send_digest_email(html: str, video_count: int):
    if not all([SMTP_USER, SMTP_PASS, DIGEST_EMAIL]):
        log.warning("Email not configured — skipping send.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Your weekly YouTube digest ({video_count} video{'s' if video_count!=1 else ''})"
    msg["From"]    = SMTP_USER
    msg["To"]      = DIGEST_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, DIGEST_EMAIL, msg.as_string())
    log.info(f"Digest sent to {DIGEST_EMAIL}")

# ---------------------------------------------------------------------------
# The main weekly job
# ---------------------------------------------------------------------------

def run_weekly_digest():
    log.info("Running weekly digest...")
    db = get_db()
    channels = db.execute("SELECT * FROM channels").fetchall()
    if not channels:
        log.info("No channels configured.")
        return

    results = []
    for ch in channels:
        try:
            videos = fetch_recent_videos(ch["channel_id"], days=7)
            log.info(f"  {ch['name']}: {len(videos)} new videos")
            for v in videos:
                already = db.execute(
                    "SELECT 1 FROM seen_videos WHERE video_id=?", (v["video_id"],)
                ).fetchone()
                if already:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO seen_videos (video_id,channel_id,title,published) VALUES (?,?,?,?)",
                    (v["video_id"], v["channel_id"], v["title"], v["published"])
                )
                db.commit()

                transcript = fetch_transcript(v["video_id"])
                if not transcript:
                    log.info(f"  Skipping {v['video_id']} (no transcript)")
                    continue

                analysis = analyse_video(v["title"], transcript)
                results.append({
                    "video_id":    v["video_id"],
                    "title":       v["title"],
                    "channel_name": ch["name"],
                    "analysis":    analysis,
                })
                db.execute("UPDATE seen_videos SET processed=1 WHERE video_id=?", (v["video_id"],))
                db.commit()
        except Exception as e:
            log.error(f"Error processing channel {ch['channel_id']}: {e}")

    if not results:
        log.info("No new videos with transcripts this week.")
        return

    html = build_digest_html(results)
    db.execute("INSERT INTO digests (video_count, html) VALUES (?,?)", (len(results), html))
    db.commit()
    send_digest_email(html, len(results))
    log.info(f"Digest complete: {len(results)} videos processed.")

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

DAY_MAP = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed",
    "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"
}

scheduler = BackgroundScheduler(timezone=pytz.utc)
scheduler.add_job(
    run_weekly_digest,
    CronTrigger(day_of_week=DAY_MAP.get(DIGEST_DAY.lower(), "mon"), hour=DIGEST_HOUR, minute=0),
    id="weekly_digest",
    replace_existing=True
)
scheduler.start()

# ---------------------------------------------------------------------------
# Web UI / API routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/channels", methods=["GET"])
def list_channels():
    db = get_db()
    rows = db.execute("SELECT * FROM channels ORDER BY added_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/channels", methods=["POST"])
def add_channel():
    data = request.json
    raw = (data.get("channel") or "").strip()
    if not raw:
        return jsonify({"error": "channel is required"}), 400
    try:
        cid, name = resolve_channel_id(raw)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    try:
        with get_db() as db:
            db.execute("INSERT INTO channels (channel_id, name) VALUES (?,?)", (cid, name))
        return jsonify({"channel_id": cid, "name": name})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Channel already added"}), 409

@app.route("/api/channels/<channel_id>", methods=["DELETE"])
def remove_channel(channel_id):
    with get_db() as db:
        db.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
    return jsonify({"ok": True})

@app.route("/api/digests", methods=["GET"])
def list_digests():
    db = get_db()
    rows = db.execute(
        "SELECT id, created_at, video_count FROM digests ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/digests/<int:digest_id>", methods=["GET"])
def get_digest(digest_id):
    db = get_db()
    row = db.execute("SELECT html FROM digests WHERE id=?", (digest_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    from flask import Response
    return Response(row["html"], mimetype="text/html")

@app.route("/api/run-now", methods=["POST"])
def run_now():
    """Manually trigger the digest (useful for testing)."""
    import threading
    threading.Thread(target=run_weekly_digest, daemon=True).start()
    return jsonify({"ok": True, "message": "Digest job started in background"})

@app.route("/api/status", methods=["GET"])
def status():
    db = get_db()
    channels  = db.execute("SELECT COUNT(*) as n FROM channels").fetchone()["n"]
    processed = db.execute("SELECT COUNT(*) as n FROM seen_videos WHERE processed=1").fetchone()["n"]
    digests   = db.execute("SELECT COUNT(*) as n FROM digests").fetchone()["n"]
    next_run  = scheduler.get_job("weekly_digest").next_run_time
    return jsonify({
        "channels":        channels,
        "videos_processed": processed,
        "digests_sent":    digests,
        "next_run":        str(next_run),
        "schedule":        f"Every {DIGEST_DAY} at {DIGEST_HOUR:02d}:00 UTC",
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
