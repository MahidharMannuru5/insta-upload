import asyncio
import base64
import io
import json
import re
import shutil
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
import streamlit as st
from playwright.async_api import async_playwright

# ---------------- UI ----------------
st.set_page_config(page_title="IG Downloader (Playwright)", page_icon="🎥", layout="centered")
st.title("🎥 Instagram Downloader (your content only)")

ig_url = st.text_input("Instagram URL (public, your content)", placeholder="https://www.instagram.com/reel/…/")
download_btn = st.button("Extract & Download")

st.divider()
st.subheader("Optional: push to your GitHub + update reels.json")
do_push = st.checkbox("Push to GitHub after download")
caption      = st.text_input("Caption", value="")
hashtags_raw = st.text_input("Hashtags (comma/space separated)", value="")
datetime_iso = st.text_input("Datetime ISO", value=datetime.now().isoformat(timespec="seconds"))
filename_hint= st.text_input("Filename hint", value="")

# ---- GitHub (secrets) ----
GITHUB_TOKEN  = st.secrets.get("GITHUB_TOKEN", "")
GITHUB_OWNER  = st.secrets.get("GITHUB_OWNER", "")
GITHUB_REPO   = st.secrets.get("GITHUB_REPO", "")
GITHUB_BRANCH = st.secrets.get("GITHUB_BRANCH", "main")
MEDIA_PATH_DIR  = st.secrets.get("MEDIA_PATH_DIR", "insta-reels/public/media")
REELS_JSON_PATH = st.secrets.get("REELS_JSON_PATH", "insta-reels/src/components/reels.json")

# ---------------- Constants ----------------
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
}
def chromium_path():
    return (shutil.which("chromium")
            or shutil.which("chromium-browser")
            or "/usr/bin/chromium")

# ---------------- Your EXACT pattern (element src) ----------------
async def extract_public_media_url(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=chromium_path(),
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # Try <video src>
            video = await page.query_selector("video")
            if video:
                video_url = await video.get_attribute("src")
                if video_url:
                    return video_url, "video"

            # Fallback <img>
            img = await page.query_selector("img[decoding='auto'], article img")
            if img:
                img_url = await img.get_attribute("src")
                if img_url:
                    return img_url, "image"

        finally:
            await context.close()
            await browser.close()
    return None, None

# ---------------- Network sniff (to avoid blob:) ----------------
async def extract_instagram_media_network(url: str, wait_seconds: int = 8):
    def looks_media(u: str):
        if not u: return False
        ul = u.lower()
        return (".mp4" in ul) or (".m3u8" in ul) or ("fbcdn" in ul and ("video" in ul or ".mp4" in ul))

    seen = set()
    found = []

    def add(u: str):
        if u and u not in seen:
            seen.add(u); found.append(u)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=chromium_path(),
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await context.new_page()

        page.on("request",  lambda req: add(req.url) if looks_media(req.url) else None)
        page.on("response", lambda res: add(res.url) if looks_media(res.url) else None)

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # try to poke play
        for sel in ["video", ".vjs-big-play-button", "button:has-text('Play')", "[autoplay]"]:
            try: await page.click(sel, timeout=1200)
            except: pass
        try:    await page.wait_for_load_state("networkidle", timeout=3000)
        except: pass
        await page.wait_for_timeout(wait_seconds * 1000)

        await context.close()
        await browser.close()

    # prefer mp4
    mp4s  = [u for u in found if ".mp4" in u.lower()]
    m3u8s = [u for u in found if ".m3u8" in u.lower()]
    best = mp4s[0] if mp4s else (m3u8s[0] if m3u8s else None)
    return best, ("video" if best else None), found

# ---------------- Download helpers ----------------
def sanitize_instagram_cdn_url(u: str) -> str:
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
    try:
        p = urlparse(u)
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        for k in list(q.keys()):
            if k.lower() in ("bytestart", "byteend", "range"):
                q.pop(k, None)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), p.fragment))
    except Exception:
        return u

def download_bytes(url: str) -> tuple[bytes, str]:
    s = requests.Session()
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
    }
    clean = sanitize_instagram_cdn_url(url)
    r = s.get(clean, headers=headers, allow_redirects=True, timeout=90, stream=True)
    if r.status_code in (403, 404, 416):
        r = s.get(url, headers=headers, allow_redirects=True, timeout=90, stream=True)
    r.raise_for_status()
    bio = io.BytesIO()
    for chunk in r.iter_content(chunk_size=65536):
        if chunk:
            bio.write(chunk)
    return bio.getvalue(), r.headers.get("content-type", "")

# ---------------- GitHub helpers (optional push) ----------------
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

def slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"(^-|-$)", "", s)
    return s[:60] or "reel"

# ---------------- Main click ----------------
if download_btn:
    if not ig_url:
        st.error("Paste an Instagram URL first.")
        st.stop()

    try:
        st.info("Step 1: extract using your original element-src logic…")
        media_url, media_type = asyncio.run(extract_public_media_url(ig_url))

        if not media_url or media_url.startswith("blob:"):
            st.warning("Got nothing or blob:. Trying network sniff…")
            media_url, media_type, all_urls = asyncio.run(extract_instagram_media_network(ig_url))
            if not media_url:
                st.error("Could not find a real .mp4/.m3u8 URL. Is the post public?")
                st.stop()
            with st.expander("Captured media-like URLs"):
                for u in all_urls:
                    st.code(u)

        st.success(f"Found {media_type}: {media_url}")

        # Download with proper headers
        media_url = sanitize_instagram_cdn_url(media_url)
        data, ct = download_bytes(media_url)
        st.caption(f"Downloaded Content-Type: {ct or 'unknown'} • Size: {len(data)//1024} KB")

        # Offer a download to the user
        st.download_button("Save file", data=data, file_name="downloaded_instagram.mp4", mime="video/mp4")

        # Optional push to GitHub + reels.json
        if do_push:
            if not all([GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO]):
                st.error("Missing GitHub secrets.")
                st.stop()

            # datetime/id
            try:
                dt = datetime.fromisoformat(datetime_iso.replace("Z", "+00:00"))
            except Exception:
                st.error("Invalid datetime. Example: 2025-08-27T12:30:00")
                st.stop()
            dt_iso = dt.isoformat(timespec="seconds")

            # filename
            ext = ".mp4" if "image" not in (ct or "") else ".jpg"
            ts   = dt_iso.replace(":", "-").replace(".", "-")
            base = slugify(filename_hint or caption or "reel")
            final_name = f"{ts}-{base}{ext}"
            media_path = f"{MEDIA_PATH_DIR}/{final_name}"

            # commit media
            b64 = base64.b64encode(data).decode("utf-8")
            gh_put_file(media_path, message=f"feat(media): add {final_name}", b64content=b64)

            # update JSON
            info = gh_get_file(REELS_JSON_PATH)
            sha  = info.get("sha") if info else None
            current = []
            if info and "content" in info:
                decoded = base64.b64decode(info["content"]).decode("utf-8")
                try:
                    current = json.loads(decoded)
                except Exception:
                    current = []
            new_id = (max([int(x.get("id", 0)) for x in current] or [0]) + 1)
            raw_url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{media_path}"
            tags = [t for t in re.split(r"[,\s]+", (hashtags_raw or "").strip()) if t]
            entry = {
                "id": new_id,
                "src": f"{raw_url}?v={dt_iso}",
                "caption": caption or "",
                "hashtags": tags,
                "datetime": dt_iso,
            }
            updated = [entry] + current
            updated_b64 = base64.b64encode(json.dumps(updated, indent=2).encode("utf-8")).decode("utf-8")
            gh_put_file(REELS_JSON_PATH, message=f"feat(reels): add {final_name} to reels.json", b64content=updated_b64, sha=sha)

            st.success("Pushed to GitHub & updated reels.json")
            st.json(entry)

    except Exception as e:
        st.error(f"Failed: {e}")
