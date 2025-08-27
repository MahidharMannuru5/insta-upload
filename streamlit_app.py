import asyncio
import io
import requests
import shutil
import streamlit as st
from playwright.async_api import async_playwright

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/119.0.0.0 Safari/537.36",
    "Range": "bytes=0-"
}

def chromium_path():
    return (shutil.which("chromium") or
            shutil.which("chromium-browser") or
            "/usr/bin/chromium")

async def extract_media(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=chromium_path(),
            args=["--no-sandbox"]
        )
        context = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            video = await page.query_selector("video")
            if video:
                src = await video.get_attribute("src")
                return src, "video"

            img = await page.query_selector("img[decoding='auto']")
            if img:
                src = await img.get_attribute("src")
                return src, "image"

        except Exception as e:
            st.error(f"Error: {e}")
        finally:
            await context.close()
            await browser.close()

    return None, None

def download_file(media_url: str):
    response = requests.get(media_url, headers=HEADERS, stream=True)
    bio = io.BytesIO()
    for chunk in response.iter_content(chunk_size=8192):
        bio.write(chunk)
    return bio.getvalue()

# Streamlit UI
st.title("🎬 Instagram Downloader")
url = st.text_input("Paste Instagram URL (reel/post)", "")

if st.button("Download"):
    if url:
        with st.spinner("Fetching media..."):
            media_url, media_type = asyncio.run(extract_media(url))

        if media_url:
            st.success(f"Found {media_type}!")
            data = download_file(media_url)
            fname = "downloaded.mp4" if media_type == "video" else "downloaded.jpg"
            mime = "video/mp4" if media_type == "video" else "image/jpeg"

            st.download_button("⬇️ Download File", data, file_name=fname, mime=mime)
        else:
            st.error("Media not found. Is the post public?")
