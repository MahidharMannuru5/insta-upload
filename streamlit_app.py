import asyncio
import io
import requests
import shutil
import streamlit as st
from playwright.async_api import async_playwright

# ---- same headers as your script ----
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/119.0.0.0 Safari/537.36",
    "Range": "bytes=0-"
}

# ---- find system chromium (installed via packages.txt on Streamlit Cloud) ----
def chromium_path():
    return (shutil.which("chromium")
            or shutil.which("chromium-browser")
            or "/usr/bin/chromium")

# ---- your same extraction logic, just with executable_path added ----
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

            # Try getting video <video src>
            video = await page.query_selector("video")
            if video:
                video_url = await video.get_attribute("src")
                if video_url:
                    return video_url, "video"

            # Fallback: try image
            img = await page.query_selector("img[decoding='auto'], article img")
            if img:
                img_url = await img.get_attribute("src")
                if img_url:
                    return img_url, "image"

        except Exception as e:
            # surface errors in UI
            st.warning(f"Extractor error: {e}")
        finally:
            await context.close()
            await browser.close()

    return None, None

# ---- your same downloader (returns bytes so we can stream to user) ----
def download_file_bytes(media_url: str):
    # Note: blob: URLs are not downloadable via requests
    if media_url.startswith("blob:"):
        raise RuntimeError("Got a blob: URL from the page. That cannot be downloaded directly.")

    r = requests.get(media_url, headers=HEADERS, stream=True, timeout=90)
    r.raise_for_status()
    bio = io.BytesIO()
    for chunk in r.iter_content(chunk_size=8192):
        if chunk:
            bio.write(chunk)
    return bio.getvalue()

# ---------------- Streamlit UI ----------------
st.set_page_config(page_title="Instagram Public Downloader", page_icon="🎬", layout="centered")
st.title("🎬 Instagram Public Downloader (same logic)")

url = st.text_input("🔗 Enter public Instagram URL (Reel/Post/Story):", placeholder="https://www.instagram.com/reel/…/")
go = st.button("Extract & Download")

if go:
    if not url:
        st.error("Please paste a URL.")
        st.stop()

    with st.spinner("Opening page and looking for media…"):
        media_url, media_type = asyncio.run(extract_public_media_url(url))

    if not media_url:
        st.error("❌ Could not extract media. Is the post public?")
        st.stop()

    st.success(f"📥 Found {media_type}: {media_url}")

    try:
        data = download_file_bytes(media_url)
    except Exception as e:
        st.error(f"Download failed: {e}")
        st.info("Tip: If you see a blob: URL, the site is using a MediaSource blob. "
                "This minimal app sticks to your original logic and won't sniff network requests.")
        st.stop()

    # filename based on type (same as your script)
    filename = "downloaded_instagram.mp4" if media_type == "video" else "downloaded_instagram.jpg"
    mime = "video/mp4" if media_type == "video" else "image/jpeg"

    st.download_button("⬇️ Save file", data=data, file_name=filename, mime=mime)
