import base64, json, re
from datetime import datetime
from urllib.parse import urlparse

import requests
import streamlit as st

# ========= CONFIG =========
GITHUB_TOKEN  = st.secrets.get("GITHUB_TOKEN", "")
GITHUB_OWNER  = st.secrets.get("GITHUB_OWNER", "MahidharMannuru5")
GITHUB_REPO   = st.secrets.get("GITHUB_REPO", "insta")
GITHUB_BRANCH = st.secrets.get("GITHUB_BRANCH", "main")

MEDIA_PATH_DIR  = st.secrets.get("MEDIA_PATH_DIR", "insta-reels/public/media")
REELS_JSON_PATH = st.secrets.get("REELS_JSON_PATH", "insta-reels/src/components/reels.json")

st.set_page_config(page_title="Upload my video to GitHub", page_icon="🎥", layout="centered")
st.title("🎥 Push my (owned) video to GitHub")

video_url = st.text_input("Direct video URL", placeholder="https://…/video.mp4")
uploaded = st.file_uploader("— or upload a local file —", type=["mp4","mov","webm","m4v"])
caption = st.text_input("Caption", "")
hashtags_raw = st.text_input("Hashtags", "love vibes")
datetime_iso = st.text_input("Datetime (ISO)", value=datetime.now().isoformat(timespec="seconds"))
filename_hint = st.text_input("Optional filename hint")
confirm = st.checkbox("I confirm I own this content.", value=False)

submit = st.button("Upload to GitHub", type="primary", disabled=not confirm)

# --- Helpers ---
def slugify(s): return re.sub(r"(^-|-$)","", re.sub(r"[^a-z0-9]+","-", (s or "").lower()))[:60] or "reel"
def ext_from_url(u): 
    for ext in (".mp4",".mov",".webm",".m4v"):
        if ext in u.lower(): return ext
    return ".mp4"
def gh_headers(): return {"Authorization":f"Bearer {GITHUB_TOKEN}","Accept":"application/vnd.github+json"}
def gh_get_file(path):
    r=requests.get(f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}",headers=gh_headers())
    return None if r.status_code==404 else r.json()
def gh_put_file(path,msg,b64,sha=None):
    body={"message":msg,"content":b64,"branch":GITHUB_BRANCH}
    if sha: body["sha"]=sha
    r=requests.put(f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}",headers=gh_headers(),data=json.dumps(body))
    r.raise_for_status(); return r.json()

# --- Action ---
if submit:
    if not GITHUB_TOKEN: st.error("Missing GitHub token in secrets."); st.stop()
    try:
        tags = [t for t in re.split(r"[,\s]+", hashtags_raw.strip()) if t]
        dt = datetime.fromisoformat(datetime_iso.replace("Z","+00:00"))
        dt_iso = dt.isoformat(timespec="seconds")
        if uploaded:
            data=uploaded.read(); ext=ext_from_url(uploaded.name)
        elif video_url:
            r=requests.get(video_url,timeout=60); r.raise_for_status()
            data=r.content; ext=ext_from_url(video_url)
        else: st.error("Need URL or upload."); st.stop()
        ts=dt_iso.replace(":","-"); base=slugify(filename_hint or caption or urlparse(video_url).path.split("/")[-1])
        final=f"{ts}-{base}{ext}"
        media_path=f"{MEDIA_PATH_DIR}/{final}"
        b64=base64.b64encode(data).decode()
        gh_put_file(media_path,f"feat(media): add {final}",b64)
        # update reels.json
        file_info=gh_get_file(REELS_JSON_PATH); sha=file_info.get("sha") if file_info else None
        current=[]
        if file_info and "content" in file_info:
            decoded=base64.b64decode(file_info["content"]).decode()
            try: current=json.loads(decoded)
            except: current=[]
        new_entry={"id": (max([int(r.get("id",0)) for r in current] or [0])+1),
                   "src": f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}/{media_path}?v={dt_iso}",
                   "caption": caption,"hashtags":tags,"datetime":dt_iso}
        updated=[new_entry]+current
        updated_b64=base64.b64encode(json.dumps(updated,indent=2).encode()).decode()
        gh_put_file(REELS_JSON_PATH,f"feat(reels): add {final} to reels.json",updated_b64,sha)
        st.success("✅ Uploaded & reels.json updated")
        st.json(new_entry)
    except Exception as e: st.error(f"Failed: {e}")
