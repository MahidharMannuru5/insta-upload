import asyncio
import base64
import html
import io
import json
import re
import shutil
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
import streamlit as st
from playwright.async_api import async_playwright

# =========================
# Streamlit UI
# =========================
st.set_page_config(page_title="IG → GitHub Uploader", page_icon="🎥", layout="centered")
st.title("🎥 Instagram → GitHub (Your Content Only)")

st.markdown(
    "Paste a **public Instagram post/reel URL** you own, or a **direct video URL**, or **upload a file**. "
    "This will upload to your repo and update `reels.json`.\n\n"
    "⚠️ Use only content you own or have permission for."
)

# =========================
# Secrets / Config
# =========================
GITHUB_TOKEN  = st.secrets.get("GITHUB_TOKEN", "")
GITHUB_OWNER  = st.secrets.get("GITHUB_OWNER", "MahidharMannuru5")
GITHUB_REPO   = st.secrets.get("GITHUB_REPO", "insta")
GITHUB_BRANCH = st.secrets.get("GITHUB_BRANCH", "main")

MEDIA_PATH_DIR  = st.secrets.get("MEDIA_PATH_DIR", "insta-reels/public/media")
REELS_JSON_PATH = st.secrets.get("REELS_JSON_PATH", "insta-reels/src/components/reels.json")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Range": "bytes=0-",
}
IG_TIMEOUT = 60

# =========================
# System Chromium path (for Streamlit Cloud with packages.txt)
# =========================
def chromium_path():
    return (shutil.which("chromium")
            or shutil.which("chromium-browser")
            or "/usr/bin/chromium")

# =========================
# GitHub helpers
# =========================
def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def gh_get_file(path: str):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}"
    r = requests.get(url, headers=gh_headers(), timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def gh_put_file(path: str, message: str, b64content: str, sha: str | None = None):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    body = {"message": message, "content": b64content, "branch": GITHUB_BRANCH}
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=gh_headers(), data=json.dumps(body), timeout=120)
    r.raise_for_status()
    return r.json()

# =========================
# Utils
# =========================
def slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"(^-|-$)", "", s)
    return s[:60] or "reel"

def ext_from_url(u: str) -> str:
    u = u.lower()
    for ext in (".mp4", ".mov", ".webm", ".m4v", ".jpg", ".jpeg"):
        if u.endswith(ext) or (ext + "?") in u:
            return ext if ext != ".jpeg" else ".jpg"
    return ".mp4"

def sanitize_instagram_cdn_url(u: str) -> str:
    """Remove byte-range query params and keep signed parts intact."""
    try:
        p = urlparse(u)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        for k in list(q.keys()):
            if k.lower() in ("bytestart", "byteend", "range"):
                q.pop(k, None)
        new_q = urlencode(q, doseq=True)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))
    except Exception:
        return u

def download_bytes(url: str) -> tuple[bytes, str, int]:
    """
    Robust downloader with IG-friendly headers. Returns (content, content_type, http_status).
    """
    s = requests.Session()
    base_headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Referer": "https://www.instagram.com/",
    }

    def do_get(u):
        return s.get(u, headers=base_headers, allow_redirects=True, timeout=60, stream=True)

    clean = sanitize_instagram_cdn_url(url)
    r = do_get(clean)
    if r.status_code in (403, 404, 416):
        r = do_get(url)  # try original if clean failed

    if r.status_code >= 400:
        raise RuntimeError(f"Download failed: HTTP {r.status_code}")

    bio = io.BytesIO()
    for chunk in r.iter_content(chunk_size=1024 * 64):
        if chunk:
            bio.write(chunk)

    return bio.getvalue(), r.headers.get("content-type", ""), r.status_code

# =========================
# Fast IG HTML extraction (OpenGraph/JSON) — no browser
# =========================
def extract_from_instagram_html(url: str):
    r = requests.get(url, headers=HEADERS, timeout=IG_TIMEOUT)
    if r.status_code >= 400:
        return None, None
    text = r.text

    m = re.search(r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if not m:
        m = re.search(r'<meta[^>]+property=["\']og:video:secure_url["\'][^>]+content=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m: return html.unescape(m.group(1)), "video"

    m = re.search(r'"video_url"\s*:\s*"([^"]+\.mp4[^"]*)"', text)
    if m: return html.unescape(m.group(1)), "video"

    m = re.search(r'"contentUrl"\s*:\s*"([^"]+\.mp4[^"]*)"', text)
    if m: return html.unescape(m.group(1)), "video"

    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m: return html.unescape(m.group(1)), "image"

    m = re.search(r'"display_url"\s*:\s*"([^"]+)"', text)
    if m: return html.unescape(m.group(1)), "image"

    return None, None

# =========================
# Playwright NETWORK SNIFFER (avoids blob:)
# =========================
async def extract_instagram_media_network(ig_url: str, wait_seconds: int = 8, debug: bool = False):
    """
    Open IG page in headless Chromium, sniff network for actual media (.mp4/.m3u8).
    Returns (best_url, media_type, all_urls_list).
    """
    MEDIA_HOSTS = ("instagram.", "cdninstagram.", "fbcdn", "fna.fbcdn.net", "cdn.fb")
    def is_media(u: str):
        if not u: return False
        ul = u.lower()
        if any(h in ul for h in MEDIA_HOSTS):
            if ".mp4" in ul or ".m3u8" in ul:
                return True
            # sometimes path hints
            if "/video/" in ul or "video" in ul:
                return True
        return (".mp4" in ul) or (".m3u8" in ul)

    candidates = []
    seen = set()
    def add(u: str):
        if not u or u in seen: return
        seen.add(u); candidates.append(u)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=chromium_path(),
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await context.new_page()

        page.on("request",  lambda req: add(req.url) if is_media(req.url) else None)
        page.on("response", lambda res: add(res.url) if is_media(res.url) else None)

        await page.goto(ig_url, wait_until="domcontentloaded", timeout=60000)

        # try to poke the player
        for sel in ["video", ".vjs-big-play-button", "button:has-text('Play')", "[autoplay]"]:
            try: await page.click(sel, timeout=1200)
            except: pass

        # wait for network
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except: pass
        await page.wait_for_timeout(wait_seconds * 1000)

        await context.close()
        await browser.close()

    # prefer mp4 over m3u8; also prefer URLs without query-range
    mp4s  = [u for u in candidates if ".mp4" in u.lower()]
    m3u8s = [u for u in candidates if ".m3u8" in u.lower()]

    best = None
    if mp4s:
        # simple heuristic: prefer shortest querystring (less likely to be partial ranges)
        best = sorted(mp4s, key=lambda u: len(urlparse(u).query or ""))[0]
        media_type = "video"
    elif m3u8s:
        best = m3u8s[0]
        media_type = "video"
    else:
        best, media_type = None, None

    return best, media_type, candidates

# =========================
# Inputs
# =========================
ig_url      = st.text_input("Instagram URL (public, your content)", placeholder="https://www.instagram.com/reel/…/")
video_url   = st.text_input("Direct video URL (optional)", placeholder="https://example.com/video.mp4")
uploaded    = st.file_uploader("— or upload a local video file —", type=["mp4", "mov", "webm", "m4v"])

caption      = st.text_input("Caption", placeholder="My awesome clip #love #vibes")
hashtags_raw = st.text_input("Hashtags", placeholder="love, vibes", help="Comma or space separated")
datetime_iso = st.text_input("Datetime (ISO)", value=datetime.now().isoformat(timespec="seconds"))
filename_hint= st.text_input("Optional filename hint", placeholder="sunset-walk")

show_debug   = st.checkbox("Show debug (all captured media URLs)")
confirm      = st.checkbox("I confirm I own this content (or have explicit permission).", value=False)
go           = st.button("Upload to GitHub", type="primary", disabled=not confirm)

# =========================
# Main
# =========================
if go:
    if not GITHUB_TOKEN:
        st.error("Missing GITHUB_TOKEN in secrets.")
        st.stop()

    try:
        # hashtags list
        tags = [t for t in re.split(r"[,\s]+", (hashtags_raw or "").strip()) if t]

        # datetime ISO
        try:
            dt = datetime.fromisoformat(datetime_iso.replace("Z", "+00:00"))
        except Exception:
            st.error("Invalid datetime. Use ISO like 2025-08-27T12:30:00")
            st.stop()
        dt_iso = dt.isoformat(timespec="seconds")

        # resolve to bytes + extension
        bytes_data = None
        ext = ".mp4"

        if ig_url:
            st.info("Trying HTML extraction…")
            media_url, media_type = extract_from_instagram_html(ig_url)

            if not media_url:
                st.info("HTML failed. Sniffing network via Playwright (system Chromium)…")
                best, media_type, all_urls = asyncio.run(
                    extract_instagram_media_network(ig_url, wait_seconds=8, debug=show_debug)
                )
                if show_debug:
                    st.write("All captured media-like URLs:")
                    for u in all_urls:
                        st.code(u)
                media_url = best

            if not media_url:
                st.error("Could not extract media from that IG URL on this host. Use a direct video URL or upload the file.")
                st.stop()

            st.write(f"Found {media_type}: {media_url}")
            media_url = sanitize_instagram_cdn_url(media_url)
            data, ct, status = download_bytes(media_url)
            st.caption(f"Downloaded with HTTP {status} • Content-Type: {ct or 'unknown'}")

            bytes_data = data
            if "image" in (ct or ""):
                ext = ".jpg"
            else:
                if "webm" in (ct or ""):        ext = ".webm"
                elif "quicktime" in (ct or ""): ext = ".mov"
                else:                            ext = ".mp4"

        elif uploaded is not None:
            st.info("Using uploaded file…")
            bytes_data = uploaded.read()
            if len(bytes_data) > 95 * 1024 * 1024:
                st.error("File > 95MB (GitHub Contents API limit). Use LFS/another host.")
                st.stop()
            ext = ext_from_url(uploaded.name or "")

        elif video_url:
            st.info("Downloading direct video URL…")
            if not video_url.startswith("https://"):
                st.error("Direct video URL must start with https://")
                st.stop()
            vurl = sanitize_instagram_cdn_url(video_url)
            data, ct, status = download_bytes(vurl)
            st.caption(f"Downloaded with HTTP {status} • Content-Type: {ct or 'unknown'}")
            bytes_data = data
            if len(bytes_data) > 95 * 1024 * 1024:
                st.error("Remote file > 95MB (GitHub Contents API limit). Use LFS/another host.")
                st.stop()
            if "webm" in (ct or ""):        ext = ".webm"
            elif "quicktime" in (ct or ""): ext = ".mov"
            else:                            ext = ext_from_url(vurl)

        else:
            st.error("Provide an Instagram URL, a direct video URL, or upload a file.")
            st.stop()

        # filename & paths
        ts   = dt_iso.replace(":", "-").replace(".", "-")
        base = slugify(filename_hint or caption or (ig_url or video_url))
        final_name = f"{ts}-{base}{ext}"
        media_path = f"{MEDIA_PATH_DIR}/{final_name}"

        # commit media
        b64 = base64.b64encode(bytes_data).decode("utf-8")
        gh_put_file(media_path, message=f"feat(media): add {final_name}", b64content=b64)

        # update reels.json
        info = gh_get_file(REELS_JSON_PATH)
        sha  = info.get("sha") if info else None
        current = []
        if info and "content" in info:
            decoded = base64.b64decode(info["content"]).decode("utf-8")
            try:
                current = json.loads(decoded)
                if not isinstance(current, list):
                    current = []
            except Exception:
                current = []

        raw_url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{media_path}"
        new_entry = {
            "id": (max([int(x.get("id", 0)) for x in current] or [0]) + 1),
            "src": f"{raw_url}?v={dt_iso}",  # cache-bust for your player
            "caption": caption or "",
            "hashtags": tags,
            "datetime": dt_iso,
        }
        updated = [new_entry] + current
        updated_json = json.dumps(updated, indent=2)
        updated_b64  = base64.b64encode(updated_json.encode("utf-8")).decode("utf-8")

        gh_put_file(
            REELS_JSON_PATH,
            message=f"feat(reels): add {final_name} to reels.json",
            b64content=updated_b64,
            sha=sha,
        )

        st.success("✅ Uploaded & reels.json updated")
        st.json(new_entry)
        st.markdown(f"**Media path:** `{media_path}`")
        st.markdown(f"[Raw video URL]({raw_url})")

    except Exception as e:
        st.error(f"Failed: {e}")
