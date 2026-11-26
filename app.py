"""
xgrab: paste an X/Twitter post link, download its video.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000
"""
import hmac
import os
import re
import struct
import tempfile
import zlib
from urllib.parse import urlparse

import requests
import yt_dlp
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)


# ---- Optional: log in as you, so age-restricted ("sensitive") posts work ----
# One environment variable on your host, XGRAB_CONFIG, written as:
#     password|auth_token|ct0
# (a password you choose, then the auth_token and ct0 cookie values from a
# logged-in x.com session, separated by | characters). Treat it like a password.
_parts = [part.strip() for part in os.environ.get("XGRAB_CONFIG", "").split("|")]
CFG_PASSWORD = _parts[0] or None
CFG_TOKEN = _parts[1] if len(_parts) > 1 else ""
CFG_CT0 = _parts[2] if len(_parts) > 2 else ""


def build_cookie_file():
    token, ct0 = CFG_TOKEN, CFG_CT0
    if not (token and ct0):
        return None
    lines = ["# Netscape HTTP Cookie File"]
    for domain in (".x.com", ".twitter.com"):
        for name, value in (("auth_token", token), ("ct0", ct0)):
            lines.append(f"{domain}\tTRUE\t/\tTRUE\t2000000000\t{name}\t{value}")
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


COOKIE_FILE = build_cookie_file()


# ---- Optional: password gate, so strangers can't use your login ----
# Turns on when XGRAB_CONFIG starts with a password. The username can be anything.
@app.before_request
def require_password():
    wanted = CFG_PASSWORD
    if not wanted:
        return None
    auth = request.authorization
    if auth and hmac.compare_digest((auth.password or "").encode(), wanted.encode()):
        return None
    return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="xgrab"'})

ALLOWED_POST_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "www.x.com", "www.twitter.com"}
ALLOWED_MEDIA_HOSTS = {"video.twimg.com"}


def clean_name(text: str) -> str:
    text = re.sub(r"[^\w\- ]+", "", text or "video").strip()
    return (text[:60] or "video").replace(" ", "_")


@app.post("/api/info")
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_POST_HOSTS:
        return jsonify(error="That doesn't look like an X or Twitter link."), 400

    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if re.search(r"NSFW|age-restricted|sensitive|authentication", msg, re.I) and not COOKIE_FILE:
            return jsonify(error="This post is age-restricted. Add your X login cookies to the server to download it."), 403
        if "No video" in msg:
            return jsonify(error="This post has no video."), 404
        return jsonify(error="Couldn't read that post. It may be private, deleted, or age-restricted."), 422

    # Posts with several videos come back as a playlist
    entries = data.get("entries") or [data]
    videos = []
    for entry in entries:
        options = []
        for f in entry.get("formats", []):
            # Keep direct MP4 files only (skip HLS/DASH playlists)
            if f.get("ext") == "mp4" and f.get("protocol") in ("http", "https") and f.get("vcodec") != "none":
                options.append({"height": f.get("height") or 0, "url": f["url"]})
        options.sort(key=lambda o: o["height"], reverse=True)
        if options:
            videos.append({
                "title": clean_name(entry.get("title") or entry.get("id")),
                "thumbnail": entry.get("thumbnail"),
                "duration": entry.get("duration"),
                "options": options,
            })

    if not videos:
        return jsonify(error="No downloadable video found in that post."), 404
    return jsonify(videos=videos)


@app.get("/api/file")
def file():
    """Stream the video through this server so the browser saves it instead of playing it."""
    media_url = request.args.get("u", "")
    name = clean_name(request.args.get("name", "video"))
    if (urlparse(media_url).hostname or "") not in ALLOWED_MEDIA_HOSTS:
        return "Not allowed", 400

    upstream = requests.get(media_url, stream=True, timeout=20)
    headers = {"Content-Disposition": f'attachment; filename="{name}.mp4"'}
    if upstream.headers.get("Content-Length"):
        headers["Content-Length"] = upstream.headers["Content-Length"]
    return Response(
        stream_with_context(upstream.iter_content(chunk_size=64 * 1024)),
        content_type="video/mp4",
        headers=headers,
    )


@app.get("/")
def home():
    return PAGE


@app.get("/manifest.webmanifest")
def manifest():
    return jsonify({
        "name": "xgrab",
        "short_name": "xgrab",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#10171d",
        "theme_color": "#1f5eff",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
        # Lets you pick xgrab in Android's Share menu from the X app
        "share_target": {
            "action": "/",
            "method": "GET",
            "params": {"title": "title", "text": "text", "url": "url"},
        },
    })


def make_icon(size: int) -> bytes:
    """Draw a blue square with a white download arrow, as PNG bytes (no image files needed)."""
    bg, fg, c = (31, 94, 255), (255, 255, 255), size / 2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            u, v = (x - c) / size, (y - c) / size
            on = (
                (abs(u) < 0.06 and -0.22 < v < 0.06)      # shaft
                or (0.0 <= v < 0.2 and abs(u) < 0.2 - v)  # head
                or (0.17 < v < 0.25 and abs(u) < 0.24)    # tray
            )
            row += bytes(fg if on else bg)
        rows.append(bytes(row))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
            + chunk(b"IEND", b""))


ICONS = {192: make_icon(192), 512: make_icon(512)}


@app.get("/icon-<int:size>.png")
def icon(size):
    if size not in ICONS:
        return "Not found", 404
    return Response(ICONS[size], content_type="image/png")


@app.get("/sw.js")
def service_worker():
    js = "self.addEventListener('install',()=>self.skipWaiting());self.addEventListener('fetch',()=>{});"
    return Response(js, content_type="text/javascript")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>xgrab: save videos from X</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#1f5eff">
<style>
  :root {
    --paper: #eef1f4;
    --ink: #14212b;
    --muted: #5d6c79;
    --line: #c9d2da;
    --accent: #1f5eff;
    --accent-ink: #ffffff;
    --error: #b3261e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper: #10171d;
      --ink: #e8eef3;
      --muted: #8d9ba7;
      --line: #2a3742;
      --accent: #6c93ff;
      --accent-ink: #0b1220;
      --error: #ff8a80;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--paper); color: var(--ink); }
  body {
    font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
    line-height: 1.5;
    min-height: 100vh;
    display: grid;
    place-items: start center;
    padding: 12vh 20px 48px;
  }
  main { width: 100%; max-width: 560px; }
  h1 {
    font-size: clamp(2.4rem, 8vw, 3.6rem);
    line-height: 1;
    margin: 0 0 12px;
    letter-spacing: -0.02em;
    font-weight: 700;
  }
  p.lede { color: var(--muted); margin: 0 0 32px; font-size: 1.1rem; max-width: 44ch; }
  form { display: flex; gap: 8px; }
  input[type=url] {
    flex: 1; min-width: 0;
    font: inherit; font-family: system-ui, sans-serif; font-size: 1rem;
    padding: 14px 16px;
    border: 1.5px solid var(--line); border-radius: 8px;
    background: transparent; color: var(--ink);
  }
  input[type=url]:focus-visible, button:focus-visible, a.dl:focus-visible {
    outline: 3px solid var(--accent); outline-offset: 2px;
  }
  button, a.dl {
    font-family: system-ui, sans-serif; font-size: 1rem; font-weight: 600;
    border: 0; border-radius: 8px; cursor: pointer; text-decoration: none;
    background: var(--accent); color: var(--accent-ink);
    padding: 14px 20px;
  }
  button[disabled] { opacity: .6; cursor: progress; }
  #msg { min-height: 1.5em; margin: 14px 0 0; font-family: system-ui, sans-serif; font-size: .95rem; color: var(--muted); }
  #msg.error { color: var(--error); }
  .video { margin-top: 28px; border-top: 1.5px solid var(--line); padding-top: 20px; }
  .video img { width: 100%; border-radius: 8px; display: block; margin-bottom: 14px; }
  .video h2 { font-size: 1rem; margin: 0 0 12px; font-family: system-ui, sans-serif; color: var(--muted); font-weight: 500; }
  .opts { display: flex; flex-wrap: wrap; gap: 8px; }
  a.dl { padding: 10px 16px; }
  a.dl.alt { background: transparent; color: var(--ink); border: 1.5px solid var(--line); font-weight: 500; }
  footer { margin-top: 56px; color: var(--muted); font-size: .85rem; font-family: system-ui, sans-serif; }
  @media (max-width: 480px) { form { flex-direction: column; } }
</style>
</head>
<body>
<main>
  <h1>xgrab</h1>
  <p class="lede">Paste the link to a post on X and save its video as an MP4.</p>

  <form id="f">
    <input id="u" type="url" placeholder="https://x.com/user/status/123..." required autocomplete="off" aria-label="Post link">
    <button id="go" type="submit">Get video</button>
  </form>
  <p id="msg" role="status" aria-live="polite"></p>
  <div id="out"></div>

  <footer>Only save videos you have the right to download. Private posts can't be fetched.</footer>
</main>

<script>
const f = document.getElementById("f"), u = document.getElementById("u"),
      go = document.getElementById("go"), msg = document.getElementById("msg"),
      out = document.getElementById("out");

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");

// Opened from Android's Share menu: pull the link out and start right away
const q = new URLSearchParams(location.search);
const shared = (q.get("url") || q.get("text") || "").match(/https?:\/\/\S+/);
if (shared) {
  u.value = shared[0];
  history.replaceState(null, "", "/");
  setTimeout(() => f.requestSubmit(), 0);
}

function say(text, isError) { msg.textContent = text; msg.className = isError ? "error" : ""; }

f.addEventListener("submit", async (e) => {
  e.preventDefault();
  out.replaceChildren();
  say("Looking for the video...");
  go.disabled = true;
  try {
    const r = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: u.value })
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "Something went wrong.");
    say("");
    data.videos.forEach(render);
  } catch (err) {
    say(err.message, true);
  } finally {
    go.disabled = false;
  }
});

function render(v) {
  const box = document.createElement("div");
  box.className = "video";
  if (v.thumbnail) {
    const img = document.createElement("img");
    img.src = v.thumbnail; img.alt = "";
    box.appendChild(img);
  }
  const h = document.createElement("h2");
  h.textContent = v.duration ? `Video, ${Math.round(v.duration)} seconds` : "Video";
  box.appendChild(h);

  const opts = document.createElement("div");
  opts.className = "opts";
  v.options.forEach((o, i) => {
    const a = document.createElement("a");
    a.className = "dl" + (i ? " alt" : "");
    a.href = `/api/file?u=${encodeURIComponent(o.url)}&name=${encodeURIComponent(v.title)}`;
    a.textContent = o.height ? `Download ${o.height}p` : "Download";
    opts.appendChild(a);
  });
  box.appendChild(opts);
  out.appendChild(box);
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
