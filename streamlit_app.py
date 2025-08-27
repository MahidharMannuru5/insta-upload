import asyncio
import base64
import html
import io
import json
import re
import shutil
from datetime import datetime
from urllib.parse import urlparse

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

def download_bytes(url: str) -> tuple[bytes, str]:
    r = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=120, stream=True)
    if r.status_code >= 400:
        raise RuntimeError(f"Download failed: HTTP {r.status_code}")
    bio = io.BytesIO()
    for chunk in r.iter_content(chunk_size=8192):
        if chunk:
            bio.write(chunk)
    return bio.getvalue(), r.headers.get("content-type", "")

# =========================
# Fast IG HTML extraction (OpenGraph/JSON) — no browser
# =========================
def extract_from_instagram_html(url: str):
    r = requests.get(url, headers=HEADERS, timeout=IG_TIMEOUT)
    if r.status_code >= 400:
        return None, None
    text = r.text

    # 1) og:video / og:video:secure_url
    m = re.search(r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if not m:
        m = re.search(r'<meta[^>]+property=["\']og:video:secure_url["\'][^>]+content=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m:
        return html.unescape(m.group(1)), "video"

    # 2) "video_url":"...mp4"
    m = re.search(r'"video_url"\s*:\s*"([^"]+\.mp4[^"]*)"', text)
    if m:
        return html.unescape(m.group(1)), "video"

    # 3) "contentUrl":"...mp4"
    m = re.search(r'"contentUrl"\s*:\s*"([^"]+\.mp4[^"]*)"', text)
    if m:
        return html.unescape(m.group(1)), "video"

    # 4) image fallback
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m:
        return html.unescape(m.group(1)), "image"
    m = re.search(r'"display_url"\s*:\s*"([^"]+)"', text)
    if m:
        return html.unescape(m.group(1)), "image"

    return None, None

# =========================
# Playwright NETWORK SNIFFER (avoids blob:)
# =========================
async def extract_instagram_media_network(ig_url: str, wait_seconds: int = 8):
    """
    Open IG page in headless Chromium, sniff network for actual media (.mp4/.m3u8).
    Returns (media_url, media_type) or (None, None).
    """
    def is_media(u: str):
        if not u: return False
        ul = u.lower()
        return (".mp4" in ul) or (".m3u8" in ul) or ("fbcdn" in ul and ("video" in ul or ".mp4" in ul))

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

        # let network idle + wait a bit
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except: pass
        await page.wait_for_timeout(wait_seconds * 1000)

        await context.close()
        await browser.close()

    # prefer mp4 over m3u8
    mp4s  = [u for u in candidates if ".mp4" in u.lower()]
    m3u8s = [u for u in candidates if ".m3u8" in u.lower()]
    if mp4s:  return mp4s[0], "video"
    if m3u8s: return m3u8s[0], "video"
    return None, None

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

confirm = st.checkbox("I confirm I own this content (or have explicit permission).", value=False)
go = st.button("Upload to GitHub", type="primary", disabled=not confirm)

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
                media_url, media_type = asyncio.run(extract_instagram_media_network(ig_url))

            if not media_url:
                st.error("Could not extract media from that IG URL on this host. Use a direct video URL or upload the file.")
                st.stop()

            st.write(f"Found {media_type}: {media_url}")
            data, ct = download_bytes(media_url)
            bytes_data = data
            if media_type == "image":
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
            data, ct = download_bytes(video_url)
            bytes_data = data
            if len(bytes_data) > 95 * 1024 * 1024:
                st.error("Remote file > 95MB (GitHub Contents API limit). Use LFS/another host.")
                st.stop()
            if "webm" in (ct or ""):        ext = ".webm"
            elif "quicktime" in (ct or ""): ext = ".mov"
            else:                            ext = ext_from_url(video_url)

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
