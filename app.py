"""
yt-dlp Web GUI - backend
A tiny FastAPI server that wraps yt-dlp: paste a URL, pick a format,
download the file. No re-encoding on video merges (streams are remuxed).
Run:  ./run.sh   (or: uvicorn app:app --host 0.0.0.0 --port 8000)
"""
import os
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", str(BASE_DIR / "downloads")))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Set NO_CHECK_CERT=1 on networks with TLS-intercepting proxies.
NO_CHECK_CERT = os.environ.get("NO_CHECK_CERT", "0") == "1"
# Keep finished files for this long (seconds) before cleanup.
FILE_TTL = int(os.environ.get("FILE_TTL", "86400"))

app = FastAPI(title="yt-dlp Web GUI")

jobs: dict = {}
jobs_lock = threading.Lock()


def base_opts() -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if NO_CHECK_CERT:
        opts["nocheckcertificate"] = True
    return opts


def _with_android_fallback(opts: dict) -> dict:
    o2 = dict(opts)
    o2["extractor_args"] = {"youtube": {"player_client": ["android"]}}
    return o2


def is_bot_check(err: Exception) -> bool:
    return "Sign in to confirm you" in str(err)


from typing import Optional


def extract_info_resilient(url: str, extra_opts: Optional[dict] = None):
    """extract_info that retries with the android player client when
    YouTube throws its datacenter-IP bot check at us."""
    opts = base_opts()
    if extra_opts:
        opts.update(extra_opts)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        if not is_bot_check(e):
            raise
        with yt_dlp.YoutubeDL(_with_android_fallback(opts)) as ydl:
            return ydl.extract_info(url, download=False)


def safe_name(name: str) -> str:
    name = (name or "video").strip()
    name = re.sub(r'[\\/:*?"<>|]+', "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "video"


class FetchRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    mode: str = "video"          # "video" | "audio"
    quality: str = "best"        # "best" | 2160 | 1440 | 1080 | 720 | 480 | 360


def quality_format(q: str) -> str:
    if q == "best":
        return "bv*+ba/b"
    return f"bv*[height<={q}]+ba/b"


def pick_formats(info: dict) -> list:
    """Collapse the raw format list into one entry per height (best of each)."""
    best: dict = {}
    for f in info.get("formats") or []:
        if f.get("vcodec") in (None, "none"):
            continue
        h = f.get("height")
        if not h:
            continue
        key = int(h)
        prev = best.get(key)
        if prev is None or (f.get("tbr") or 0) > (prev.get("tbr") or 0):
            best[key] = f
    out = []
    for h in sorted(best, reverse=True):
        f = best[h]
        out.append({
            "height": h,
            "ext": f.get("ext"),
            "fps": f.get("fps"),
            "vcodec": (f.get("vcodec") or "").split(".")[0],
            "filesize": f.get("filesize") or f.get("filesize_approx"),
        })
    return out


@app.post("/api/info")
def api_info(req: FetchRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Empty URL")
    # Fast pass: is it a playlist? (flat extraction is quick)
    try:
        info = extract_info_resilient(url, {"extract_flat": "in_playlist"})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read that URL: {e}")
    if not info:
        raise HTTPException(400, "Could not read that URL")

    if info.get("_type") == "playlist" or info.get("entries"):
        entries = []
        for e in info.get("entries") or []:
            if not e:
                continue
            thumbs = e.get("thumbnails") or []
            entries.append({
                "id": e.get("id"),
                "title": e.get("title") or "Untitled",
                "url": e.get("url") or e.get("webpage_url"),
                "duration": e.get("duration"),
                "thumbnail": thumbs[-1].get("url") if thumbs else None,
            })
        return {
            "type": "playlist",
            "title": info.get("title") or "Playlist",
            "uploader": info.get("uploader"),
            "count": len(entries),
            "entries": entries,
        }

    # Single video: full extraction so we can list real formats.
    try:
        full = extract_info_resilient(url)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not read that URL: {e}")
    thumbs = full.get("thumbnails") or []
    return {
        "type": "video",
        "title": full.get("title") or "Untitled",
        "uploader": full.get("uploader"),
        "duration": full.get("duration"),
        "thumbnail": thumbs[-1].get("url") if thumbs else None,
        "webpage_url": full.get("webpage_url") or url,
        "formats": pick_formats(full),
    }


def run_download(job_id: str, url: str, mode: str, quality: str):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j["percent"] = round(done / total * 100, 1) if total else 0
                    j["speed"] = d.get("speed")
                    j["eta"] = d.get("eta")
        elif d["status"] == "finished":
            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j["status"] = "processing"
                    j["percent"] = 100

    opts = base_opts()
    opts["progress_hooks"] = [hook]

    # Grab the title first so the saved file gets a nice name.
    try:
        meta = extract_info_resilient(url)
        title = safe_name(meta.get("title") if meta else None)
    except Exception:  # noqa: BLE001
        title = "video"
    stem = f"{title}-{job_id[:6]}"
    opts["outtmpl"] = str(DOWNLOAD_DIR / f"{stem}.%(ext)s")

    if mode == "audio":
        opts["format"] = "ba/b"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        opts["format"] = quality_format(quality)
        opts["merge_output_format"] = "mp4"  # remux only, no re-encode

    def _do_download(dl_opts: dict):
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([url])

    try:
        try:
            _do_download(opts)
        except Exception as e:  # noqa: BLE001
            if not is_bot_check(e):
                raise
            _do_download(_with_android_fallback(opts))
        files = sorted(DOWNLOAD_DIR.glob(f"{stem}.*"))
        with jobs_lock:
            j = jobs.get(job_id)
            if j:
                if files:
                    j["status"] = "done"
                    j["file"] = files[0].name
                    j["title"] = title
                    j["finished_at"] = time.time()
                else:
                    j["status"] = "error"
                    j["error"] = "Download finished but no file was produced."
    except Exception as e:  # noqa: BLE001
        with jobs_lock:
            j = jobs.get(job_id)
            if j:
                j["status"] = "error"
                j["error"] = str(e)[:300]


@app.post("/api/download")
def api_download(req: DownloadRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Empty URL")
    if req.mode not in ("video", "audio"):
        raise HTTPException(400, "mode must be 'video' or 'audio'")
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id, "url": url, "mode": req.mode, "quality": req.quality,
            "status": "downloading", "percent": 0, "speed": None, "eta": None,
            "title": None, "file": None, "error": None, "created": time.time(),
        }
    threading.Thread(target=run_download, args=(job_id, url, req.mode, req.quality),
                     daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    with jobs_lock:
        j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Unknown job")
    return j


@app.get("/api/file/{job_id}")
def api_file(job_id: str):
    with jobs_lock:
        j = jobs.get(job_id)
    if not j or j["status"] != "done" or not j.get("file"):
        raise HTTPException(404, "File not ready")
    path = DOWNLOAD_DIR / j["file"]
    if not path.exists():
        raise HTTPException(404, "File expired")
    return FileResponse(path, filename=path.name)


@app.on_event("startup")
def _cleanup_loop():
    def loop():
        while True:
            time.sleep(600)
            now = time.time()
            with jobs_lock:
                stale = [jid for jid, j in jobs.items()
                         if j.get("finished_at") and now - j["finished_at"] > FILE_TTL]
                for jid in stale:
                    f = jobs[jid].get("file")
                    if f:
                        try:
                            (DOWNLOAD_DIR / f).unlink(missing_ok=True)
                        except OSError:
                            pass
                    del jobs[jid]
    threading.Thread(target=loop, daemon=True).start()


app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
