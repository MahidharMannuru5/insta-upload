import base64
import io
import json
import os
import re
import subprocess
from datetime import datetime
from urllib.parse import urlparse

import requests
import streamlit as st

# Try Playwright imports; we’ll install the browser if needed on first use
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except Exception:
    PLAYWRIGHT_OK = False

# ========= CONFIG via Streamlit Secrets =========
GITHUB_TOKEN  = st.secrets.get("GITHUB_TOKEN", "")
GITHUB_OWNER  = st.secrets.get("GITHUB_OWNER", "MahidharMannuru5")
GITHUB_REPO   = st.secrets.get("GITHUB_REPO", "insta")
GITHUB_BRANCH = st.secrets.get("GITHUB_BRANCH", "main")

MEDIA_PATH_DIR  = st.secrets.get("MEDIA_PATH_DIR", "insta-reels/public/media")
REELS_JSON_PATH = st.secrets.get("REELS_JSON_PATH", "insta-reels/src/components/reels.json")

# ========= PAGE =========
st.set_page_config(page_title="Upload my video to GitHub", page_icon="🎥", layout="centered")
st.title("🎥 Push my (owned) video to GitHub")

st.markdown(
    "Use **one** of the inputs below:\n\n"
    "1) Paste an **Instagram post/reel URL** that you own → the app will extract and upload.\n\n"
    "2) Paste a **direct video URL** (https://…/file.mp4) **or** upload a local file.\n"
)

# ----------- UI -----------
ig_url = st.text_input("Instagram URL (post/reel you own)", placeholder="https://www.instagram.com/reel/…/")
st.caption("If you use an Instagram URL, the app will try to extract the media automatically (Playwright).")

st.divider()
video_url = st.text_input("Direct video URL (optional)", placeholder="https://example.com/video.mp4")
uploaded = st.file_uploader("— or upload a local video file —", type=["mp4", "mov", "webm", "m4v"])

caption = st.text_input("Caption", placeholder="My awesome clip #love #vibes")
hashtags_raw = st.text_input("Hashtags", placeholder="love, vibes", help="Comma or space separated")
datetime_iso = st.text_input("Datetime (ISO)", value=datetime.now().isoformat(timespec="seconds"))
filename_hint = st.text_input("Optional filename hint", placeholder="sunset-walk")

confirm = st.checkbox("I confirm I own this content (or have explicit permission).", value=False)
submit = st.button("Upload to GitHub", type="primary", disabled=not confirm)

# ----------- Helpers -----------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/119.0.0.0 Safari/537.36",
    "Range": "bytes=0-",
}

def slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"(^-|-$)", "", s)
    return s[:60] or "reel"

def is_video_content_type(ct: str) -> bool:
    return ct.startswith("video/")

def ext_from_url(u: str) -> str:
    u = u.lower()
    for ext in (".mp4", ".mov", ".webm", ".m4v"):
        if u.endswith(ext) or (ext + "?") in u:
            return ext
    return ".mp4"

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
    body = {
        "message": message,
        "content": b64content,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=gh_headers(), data=json.dumps(body), timeout=120)
    if r.status_code >= 400:
        st.error(r.text)
    r.raise_for_status()
    return r.json()

def ensure_chromium_installed():
    """
    On first run in Streamlit Cloud, ensure the Playwright browser is installed.
    Safe to call multiple times.
    """
    try:
        # try a quick launch to see if chromium is present
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        pass

    try:
        # install chromium quietly
        subprocess.run(["playwright", "install", "chromium"], check=False, capture_output=True)
        # verify
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception as e:
        st.warning(f"Could not auto-install Chromium: {e}")
        return False

def extract_media_from_instagram_sync(url: str):
    """
    Your extractor logic merged (sync version).
    Returns (media_url, media_type) where media_type is 'video' or 'image', or (None, None).
    """
    if not PLAYWRIGHT_OK:
        return None, None
    if not ensure_chromium_installed():
        return None, None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()

        media_url, media_type = None, None
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)

            # try <video>
            video = page.query_selector("video")
            if video:
                vurl = video.get_attribute("src")
                if vurl:
                    media_url, media_type = vurl, "video"
                    return media_url, media_type

            # try <img> fallback
            img = page.query_selector("img[decoding='auto'], img[srcset], article img")
            if img:
                iurl = img.get_attribute("src")
                if iurl:
                    media_url, media_type = iurl, "image"
                    return media_url, media_type
        except Exception as e:
            st.warning(f"Extractor failed: {e}")
        finally:
            context.close()
            browser.close()

    return None, None

def download_bytes(url: str) -> tuple[bytes, str]:
    r = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=120, stream=True)
    if r.status_code >= 400:
        raise RuntimeError(f"Download failed: HTTP {r.status_code}")
    # If streaming, gather into bytes
    bio = io.BytesIO()
    for chunk in r.iter_content(chunk_size=8192):
        if chunk:
            bio.write(chunk)
    return bio.getvalue(), r.headers.get("content-type", "")

# ----------- Action -----------
if submit:
    if not GITHUB_TOKEN:
        st.error("Missing GITHUB_TOKEN in Streamlit secrets.")
        st.stop()

    try:
        # 1) Parse hashtags
        if hashtags_raw.strip():
            tags = re.split(r"[,\s]+", hashtags_raw.strip())
            tags = [t for t in tags if t]
        else:
            tags = []

        # 2) Validate datetime
        try:
            dt = datetime.fromisoformat(datetime_iso.replace("Z", "+00:00"))
        except Exception:
            st.error("Invalid datetime. Use ISO like 2025-08-27T12:30:00")
            st.stop()
        dt_iso = dt.isoformat(timespec="seconds")

        # 3) Resolve the source into bytes + extension
        bytes_data = None
        ext = ".mp4"

        if ig_url:
            st.info("Extracting media from Instagram (this must be your own content).")
            media_url, media_type = extract_media_from_instagram_sync(ig_url)
            if not media_url:
                st.error("Could not extract media from the Instagram URL. Ensure the post is public and try again.")
                st.stop()
            st.write(f"Found {media_type}: {media_url}")
            data, ct = download_bytes(media_url)
            bytes_data = data
            if media_type == "image":
                ext = ".jpg"
            else:
                # if content-type suggests a different video container
                if "webm" in ct:
                    ext = ".webm"
                elif "quicktime" in ct or "mov" in ct:
                    ext = ".mov"
                else:
                    ext = ".mp4"

        elif uploaded is not None:
            st.info("Using uploaded file…")
            bytes_data = uploaded.read()
            if len(bytes_data) > 95 * 1024 * 1024:
                st.error("File > 95MB (GitHub API limit). Use LFS/another host.")
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
                st.error("Remote file > 95MB (GitHub API limit). Use LFS/another host.")
                st.stop()
            # derive ext
            if "webm" in ct:
                ext = ".webm"
            elif "quicktime" in ct or "mov" in ct:
                ext = ".mov"
            else:
                ext = ext_from_url(video_url)

        else:
            st.error("Provide an Instagram URL, a direct video URL, or upload a file.")
            st.stop()

        # 4) Build filename & paths
        ts = dt_iso.replace(":", "-").replace(".", "-")
        base = slugify(filename_hint or caption or (ig_url or video_url))
        final_name = f"{ts}-{base}{ext}"
        media_path = f"{MEDIA_PATH_DIR}/{final_name}"

        # 5) Commit media file
        b64 = base64.b64encode(bytes_data).decode("utf-8")
        gh_put_file(
            media_path,
            message=f"feat(media): add {final_name}",
            b64content=b64,
        )

        # 6) Read existing reels.json (get sha)
        file_info = gh_get_file(REELS_JSON_PATH)
        sha = None
        current = []
        if file_info and "content" in file_info:
            sha = file_info.get("sha")
            decoded = base64.b64decode(file_info["content"]).decode("utf-8")
            try:
                current = json.loads(decoded)
                if not isinstance(current, list):
                    current = []
            except Exception:
                current = []

        # 7) Build entry and prepend
        raw_url = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{media_path}"
        new_entry = {
            "id": (max([int(x.get("id", 0)) for x in current] or [0]) + 1),
            "src": f"{raw_url}?v={dt_iso}",  # cache-bust in your player
            "caption": caption or "",
            "hashtags": tags,
            "datetime": dt_iso,
        }
        updated = [new_entry] + current
        updated_json = json.dumps(updated, indent=2)
        updated_b64 = base64.b64encode(updated_json.encode("utf-8")).decode("utf-8")

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
