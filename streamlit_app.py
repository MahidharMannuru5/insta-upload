import streamlit as st
from playwright.sync_api import sync_playwright
import os
import json
import httpx
import base64
from datetime import datetime

# --- HEADERS (same as local script) ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/119.0.0.0 Safari/537.36",
    "Range": "bytes=0-"
}

# --- GitHub Setup ---
GITHUB_TOKEN = st.secrets["GITHUB_TOKEN"]
GITHUB_USERNAME = st.secrets["GITHUB_USERNAME"]
GITHUB_REPO = st.secrets["GITHUB_REPO"]
REEL_JSON_PATH = st.secrets["REEL_JSON_PATH"]
MEDIA_DIR_PATH = st.secrets["MEDIA_DIR_PATH"]

GH_API_URL = f"https://api.github.com/repos/{GITHUB_USERNAME}/{GITHUB_REPO}/contents"
CDN_BASE = f"https://raw.githubusercontent.com/{GITHUB_USERNAME}/{GITHUB_REPO}/main/{MEDIA_DIR_PATH}"

# --- UI ---
st.title("📥 Instagram Reel Downloader")
url = st.text_input("Paste public Instagram URL (Reel/Post/Story):")
caption = st.text_input("Caption:")
hashtags = st.text_input("Hashtags (comma-separated):")
filename_hint = st.text_input("Filename hint (optional):")

if st.button("Download and Upload"):
    if not url:
        st.warning("Please provide an Instagram URL.")
        st.stop()

    with st.spinner("🔍 Fetching media..."):

        # Extract using playwright
        def extract_url_sync(insta_url):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=HEADERS["User-Agent"])
                page = context.new_page()
                try:
                    page.goto(insta_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                    video = page.query_selector("video")
                    if video:
                        return video.get_attribute("src"), "video"
                    img = page.query_selector("img[decoding='auto']")
                    if img:
                        return img.get_attribute("src"), "image"
                except Exception as e:
                    st.error(f"Playwright error: {e}")
                finally:
                    browser.close()
            return None, None

        media_url, media_type = extract_url_sync(url)

        if not media_url:
            st.error("❌ Failed to extract media. Is the link public?")
            st.stop()

        ext = ".mp4" if media_type == "video" else ".jpg"
        timestamp = datetime.now().isoformat(timespec="seconds")
        filename = f"{timestamp.replace(':', '-')}-{filename_hint or 'downloaded'}{ext}"

        # Download bytes using local headers
        try:
            with httpx.stream("GET", media_url, headers=HEADERS, follow_redirects=True, timeout=60.0, verify=False) as r:
                content_type = r.headers.get("content-type", "")
                content = b"".join([chunk for chunk in r.iter_bytes()])
        except Exception as e:
            st.error(f"Download failed: {e}")
            st.stop()

        # Upload file to GitHub
        def github_upload(filepath, content_bytes, commit_msg):
            url = f"{GH_API_URL}/{filepath}"
            b64 = base64.b64encode(content_bytes).decode()
            payload = {
                "message": commit_msg,
                "content": b64
            }

            # Check if file exists (for SHA)
            check = httpx.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"})
            if check.status_code == 200:
                payload["sha"] = check.json()["sha"]

            r = httpx.put(url, headers={"Authorization": f"token {GITHUB_TOKEN}"}, json=payload)
            r.raise_for_status()

        try:
            github_upload(f"{MEDIA_DIR_PATH}/{filename}", content, f"add: {filename}")
            st.success("✅ Uploaded media to GitHub.")
        except Exception as e:
            st.error(f"Upload failed: {e}")
            st.stop()

        # Update reels.json
        try:
            json_url = f"{GH_API_URL}/{REEL_JSON_PATH}"
            r = httpx.get(json_url, headers={"Authorization": f"token {GITHUB_TOKEN}"})
            existing = []
            sha = None
            if r.status_code == 200:
                sha = r.json()["sha"]
                existing = json.loads(httpx.get(r.json()["download_url"]).text)

            new_id = max([item["id"] for item in existing], default=0) + 1
            new_entry = {
                "id": new_id,
                "src": f"{CDN_BASE}/{filename}?v={timestamp}",
                "caption": caption,
                "hashtags": [tag.strip() for tag in hashtags.split(",") if tag.strip()],
                "datetime": timestamp
            }
            existing.insert(0, new_entry)
            updated_json = json.dumps(existing, indent=2)
            github_upload(REEL_JSON_PATH, updated_json.encode(), f"update: reels.json with {filename}")
            st.success("📁 Updated reels.json")
        except Exception as e:
            st.error(f"JSON update failed: {e}")
            st.stop()

        st.video(f"{CDN_BASE}/{filename}?v={timestamp}")
