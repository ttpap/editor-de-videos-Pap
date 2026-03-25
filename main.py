import os
import re
import json
import uuid
import shutil
import subprocess
import traceback
import urllib.request
import urllib.parse
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Editor de Vídeos")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
STATIC_DIR = BASE_DIR / "static"

for d in [UPLOAD_DIR, OUTPUT_DIR, STATIC_DIR]:
    d.mkdir(exist_ok=True)

jobs: dict = {}

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv", ".3gp", ".ts", ".mts", ".m2ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".heic", ".heif"}

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")


@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/api/session")
async def create_session():
    session_id = str(uuid.uuid4())
    (UPLOAD_DIR / session_id).mkdir()
    return {"session_id": session_id}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), session_id: str = Form(...)):
    session_dir = UPLOAD_DIR / session_id
    if not session_dir.exists():
        session_dir.mkdir(parents=True)

    safe_name = re.sub(r"[^\w\-_\. ]", "_", file.filename or "arquivo")
    file_path = session_dir / safe_name

    # Handle duplicates
    counter = 1
    while file_path.exists():
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        file_path = session_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    content = await file.read()
    file_path.write_bytes(content)

    ext = file_path.suffix.lower()
    media_type = "video" if ext in VIDEO_EXT else "image" if ext in IMAGE_EXT else "unknown"

    return {
        "name": file_path.name,
        "size": len(content),
        "type": media_type,
        "session_id": session_id,
    }


@app.get("/api/music")
async def search_music(mood: str = "all", q: str = ""):
    """Busca músicas royalty-free no ccMixter (CC-licensed, uso livre)."""
    try:
        mood_tags = {
            "energetico": "upbeat,energetic,dance",
            "calmo":      "calm,ambient,relaxing",
            "cinematico": "cinematic,epic,film",
            "alegre":     "happy,positive,fun",
            "romantico":  "romantic,love,soft",
            "all":        "instrumental",
        }
        tags = mood_tags.get(mood, "instrumental")
        if q:
            tags = q

        params = urllib.parse.urlencode({
            "format": "json",
            "limit": 24,
            "tags": tags,
            "type": "0",   # instrumental uploads only
            "lic": "1",    # verified license
        })
        url = f"http://ccmixter.org/api/query?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "VideoEditor/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())

        tracks = []
        for item in data:
            dl = item.get("files", [{}])[0].get("file_download_url", "")
            if not dl or not dl.endswith(".mp3"):
                # try any mp3 in files list
                for f in item.get("files", []):
                    if f.get("file_download_url", "").endswith(".mp3"):
                        dl = f["file_download_url"]
                        break
            if not dl:
                continue
            tracks.append({
                "id": str(item.get("upload_id", "")),
                "title": item.get("upload_name", "Sem título"),
                "artist": item.get("user_name", "Desconhecido"),
                "url": dl,
                "license": item.get("license_name", "CC"),
                "duration": item.get("upload_duration", 0),
                "page": item.get("upload_page_url", ""),
            })

        return {"tracks": tracks, "source": "ccMixter (CC Licensed)"}

    except Exception as e:
        # Fallback: curated CC0 tracks from Pixabay CDN (known working URLs)
        fallback = [
            {"id":"pb1","title":"Inspiring Cinematic","artist":"Lesfm","url":"https://cdn.pixabay.com/audio/2022/10/16/audio_12a0e684f2.mp3","license":"CC0","duration":185,"page":"https://pixabay.com/music/"},
            {"id":"pb2","title":"Lofi Study","artist":"FASSounds","url":"https://cdn.pixabay.com/audio/2022/01/18/audio_d0c6ff1fbe.mp3","license":"CC0","duration":132,"page":"https://pixabay.com/music/"},
            {"id":"pb3","title":"Upbeat Corporate","artist":"Audiorezout","url":"https://cdn.pixabay.com/audio/2022/08/04/audio_2dde668d05.mp3","license":"CC0","duration":152,"page":"https://pixabay.com/music/"},
            {"id":"pb4","title":"Happy Acoustic","artist":"Lesfm","url":"https://cdn.pixabay.com/audio/2021/11/13/audio_a94e4e0bce.mp3","license":"CC0","duration":120,"page":"https://pixabay.com/music/"},
            {"id":"pb5","title":"Ambient Chill","artist":"Music_Unlimited","url":"https://cdn.pixabay.com/audio/2022/03/10/audio_c8c8a73467.mp3","license":"CC0","duration":210,"page":"https://pixabay.com/music/"},
        ]
        return {"tracks": fallback, "source": "Pixabay (CC0)", "fallback": True}


@app.post("/api/process")
async def process_video(
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    duration: int = Form(...),
    files_order: str = Form(...),
    transition: str = Form(default="fade"),
    resolution: str = Form(default="1080p"),
    music_url: str = Form(default=""),
    music_volume: float = Form(default=0.3),
):
    files = json.loads(files_order)
    if not files:
        raise HTTPException(status_code=400, detail="Nenhum arquivo selecionado")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing", "progress": 0, "message": "Iniciando..."}
    background_tasks.add_task(
        create_video, job_id, session_id, duration, files, transition, resolution,
        music_url, music_volume
    )
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    return jobs[job_id]


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    if jobs[job_id].get("status") == "processing":
        jobs[job_id]["cancel_requested"] = True
        jobs[job_id]["message"] = "Cancelando..."
    return {"ok": True}


@app.get("/api/download/{job_id}")
async def download(job_id: str):
    output_file = OUTPUT_DIR / f"{job_id}.mp4"
    if not output_file.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    job = jobs.get(job_id, {})
    filename = job.get("filename", "video_editado.mp4")
    return FileResponse(
        str(output_file),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def run(cmd: list, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def get_duration(path: str) -> float:
    try:
        r = run([
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ])
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def has_audio_stream(path: str) -> bool:
    r = run([
        "ffprobe", "-v", "quiet",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "default=noprint_wrappers=1",
        path,
    ], check=False)
    return bool(r.stdout.strip())


def scale_filter(w: int, h: int) -> str:
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,format=yuv420p"
    )


RESOLUTIONS = {
    "720p":  (1280, 720),
    "1080p": (1920, 1080),
}

TRANSITIONS = [
    "fade", "dissolve", "wipeleft", "wiperight",
    "slideleft", "slideright", "smoothleft", "smoothright",
]


# ─── Core processing ──────────────────────────────────────────────────────────

def create_video(
    job_id: str,
    session_id: str,
    duration_minutes: int,
    files_order: list,
    transition: str,
    resolution: str,
    music_url: str = "",
    music_volume: float = 0.3,
):
    temp_dir = UPLOAD_DIR / f"temp_{job_id}"
    temp_dir.mkdir(exist_ok=True)
    session_dir = UPLOAD_DIR / session_id
    output_file = OUTPUT_DIR / f"{job_id}.mp4"

    try:
        w, h = RESOLUTIONS.get(resolution, (1920, 1080))
        target_secs = duration_minutes * 60

        # ── Collect media files ──────────────────────────────────────────────
        media = []
        for fname in files_order:
            fpath = session_dir / fname
            if not fpath.exists():
                continue
            ext = fpath.suffix.lower()
            if ext in VIDEO_EXT:
                media.append({"path": str(fpath), "type": "video", "name": fname})
            elif ext in IMAGE_EXT:
                media.append({"path": str(fpath), "type": "image", "name": fname})

        if not media:
            jobs[job_id] = {"status": "error", "message": "Nenhum arquivo válido encontrado."}
            return

        # ── Calculate timing ─────────────────────────────────────────────────
        video_count = sum(1 for m in media if m["type"] == "video")
        image_count = sum(1 for m in media if m["type"] == "image")

        total_vid_dur = 0.0
        for m in media:
            if m["type"] == "video":
                d = get_duration(m["path"])
                m["orig_dur"] = d
                total_vid_dur += d
            else:
                m["orig_dur"] = 0.0

        # Image duration: fill remaining time evenly, capped between 3s and 12s
        if image_count > 0:
            remaining = max(target_secs - total_vid_dur, image_count * 3)
            img_dur = max(3.0, min(12.0, remaining / image_count))
        else:
            img_dur = 5.0

        # Speed up/slow down videos if content is too long or too short
        total_content = total_vid_dur + image_count * img_dur
        if total_content > target_secs * 1.05 and total_vid_dur > 0:
            video_speed = total_vid_dur / max(target_secs - image_count * img_dur, 1)
            video_speed = round(max(0.5, min(3.0, video_speed)), 3)
        else:
            video_speed = 1.0

        vf_scale = scale_filter(w, h)
        trans_dur = 0.8  # seconds for each transition

        # ── Convert each file to standardized clip ───────────────────────────
        clip_paths = []
        for i, m in enumerate(media):
            pct = int((i / len(media)) * 70)
            jobs[job_id] = {
                "status": "processing",
                "progress": pct,
                "message": f"Processando {i+1}/{len(media)}: {m['name']}",
            }

            clip_out = str(temp_dir / f"clip_{i:04d}.mp4")

            if m["type"] == "image":
                # Ken Burns zoom effect for images
                zoom_vf = (
                    f"zoompan=z='min(zoom+0.0005,1.04)':d={int(img_dur * 30)}"
                    f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h},"
                    f"setsar=1,format=yuv420p"
                )
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-loop", "1", "-i", m["path"],
                    "-f", "lavfi", "-i", "aevalsrc=0:c=stereo:r=44100",
                    "-c:v", "libx264",
                    "-t", str(img_dur),
                    "-vf", zoom_vf,
                    "-r", "30",
                    "-c:a", "aac", "-b:a", "64k",
                    "-preset", "fast", "-shortest",
                    clip_out,
                ]
                r = run(cmd, check=False)
                if r.returncode != 0:
                    # Fallback without Ken Burns
                    cmd[-8] = vf_scale
                    r = run(cmd, check=False)
                if r.returncode != 0:
                    raise RuntimeError(f"Erro ao processar imagem {m['name']}:\n{r.stderr}")

            else:
                # Video file
                vf = vf_scale
                af = "anull"

                if video_speed != 1.0:
                    vf = f"{vf_scale},setpts={1/video_speed:.4f}*PTS"
                    spd = video_speed
                    if spd <= 2.0:
                        af = f"atempo={spd:.3f}"
                    elif spd <= 4.0:
                        af = f"atempo=2.0,atempo={spd/2.0:.3f}"
                    else:
                        af = "atempo=2.0,atempo=2.0"

                has_aud = has_audio_stream(m["path"])

                base_cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", m["path"],
                ]
                if not has_aud:
                    base_cmd += ["-f", "lavfi", "-i", "aevalsrc=0:c=stereo:r=44100"]

                base_cmd += [
                    "-c:v", "libx264",
                    "-vf", vf,
                    "-r", "30",
                    "-c:a", "aac",
                    "-b:a", "128k" if has_aud else "64k",
                    "-af", af,
                    "-preset", "fast",
                ]
                if not has_aud:
                    base_cmd += ["-shortest"]
                base_cmd.append(clip_out)

                r = run(base_cmd, check=False)
                if r.returncode != 0:
                    raise RuntimeError(f"Erro ao processar vídeo {m['name']}:\n{r.stderr}")

            clip_paths.append(clip_out)

            # Check for cancel
            if jobs[job_id].get("cancel_requested"):
                jobs[job_id] = {"status": "cancelled", "progress": 0, "message": "Processamento cancelado."}
                return

        # ── Merge clips ──────────────────────────────────────────────────────
        if jobs[job_id].get("cancel_requested"):
            jobs[job_id] = {"status": "cancelled", "progress": 0, "message": "Processamento cancelado."}
            return
        jobs[job_id] = {"status": "processing", "progress": 75, "message": "Montando vídeo final..."}

        if len(clip_paths) == 1:
            shutil.copy(clip_paths[0], str(output_file))
        else:
            clip_durations = [get_duration(p) for p in clip_paths]
            _merge(clip_paths, clip_durations, str(output_file), transition, target_secs)

        # ── Mix background music ──────────────────────────────────────────────
        if music_url:
            jobs[job_id] = {"status": "processing", "progress": 85, "message": "Baixando e mixando música..."}
            music_path = str(temp_dir / "bg_music.mp3")
            try:
                req = urllib.request.Request(music_url, headers={"User-Agent": "VideoEditor/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    Path(music_path).write_bytes(resp.read())

                mixed_path = str(output_file).replace(".mp4", "_mixed.mp4")
                vid_dur = get_duration(str(output_file))
                # Mix: loop music if shorter than video, duck original audio, fade music out
                r_mix = run([
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(output_file),
                    "-stream_loop", "-1", "-i", music_path,
                    "-filter_complex",
                    f"[0:a]volume=1.0[va];[1:a]volume={music_volume:.2f},afade=t=out:st={max(0,vid_dur-3):.1f}:d=3[bga];[va][bga]amix=inputs=2:duration=first[aout]",
                    "-map", "0:v",
                    "-map", "[aout]",
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "128k",
                    "-t", str(vid_dur),
                    mixed_path,
                ], check=False)

                if r_mix.returncode == 0:
                    os.replace(mixed_path, str(output_file))
                else:
                    # Try without original audio (images only case)
                    r_mix2 = run([
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-i", str(output_file),
                        "-stream_loop", "-1", "-i", music_path,
                        "-filter_complex",
                        f"[1:a]volume={music_volume:.2f},afade=t=out:st={max(0,vid_dur-3):.1f}:d=3[bga]",
                        "-map", "0:v",
                        "-map", "[bga]",
                        "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "128k",
                        "-t", str(vid_dur),
                        mixed_path,
                    ], check=False)
                    if r_mix2.returncode == 0:
                        os.replace(mixed_path, str(output_file))
            except Exception as me:
                pass  # Music failed silently — video still delivered without it

        # ── Done ─────────────────────────────────────────────────────────────
        jobs[job_id] = {"status": "processing", "progress": 95, "message": "Finalizando..."}
        actual_dur = get_duration(str(output_file))
        fsize = output_file.stat().st_size

        filename = f"video_{duration_minutes}min_{resolution}.mp4"
        jobs[job_id] = {
            "status": "done",
            "progress": 100,
            "message": "Vídeo criado com sucesso!",
            "job_id": job_id,
            "filename": filename,
            "duration_sec": round(actual_dur),
            "file_size_mb": round(fsize / 1024 / 1024, 1),
        }

    except Exception as e:
        jobs[job_id] = {
            "status": "error",
            "message": str(e),
            "detail": traceback.format_exc(),
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        shutil.rmtree(session_dir, ignore_errors=True)


def _merge(clip_paths: list, clip_durations: list, output: str, transition: str, target_secs: int):
    n = len(clip_paths)
    trans_dur = min(0.8, min(clip_durations) * 0.4)

    # Build ffmpeg inputs
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in clip_paths:
        cmd += ["-i", p]

    # Build xfade + acrossfade filter graph
    v_chain = "[0:v]"
    a_chain = "[0:a]"
    filters = []
    offset = 0.0

    for i in range(n - 1):
        offset += clip_durations[i] - trans_dur

        if transition == "random":
            t = TRANSITIONS[i % len(TRANSITIONS)]
        else:
            t = transition if transition in TRANSITIONS else "fade"

        v_out = f"[vx{i}]"
        a_out = f"[ax{i}]"
        next_v = f"[{i+1}:v]"
        next_a = f"[{i+1}:a]"

        filters.append(
            f"{v_chain}{next_v}xfade=transition={t}:duration={trans_dur:.3f}:offset={offset:.3f}{v_out}"
        )
        filters.append(
            f"{a_chain}{next_a}acrossfade=d={trans_dur:.3f}:c1=tri:c2=tri{a_out}"
        )
        v_chain = v_out
        a_chain = a_out

    fc = ";".join(filters)

    cmd += [
        "-filter_complex", fc,
        "-map", v_chain,
        "-map", a_chain,
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "slow",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-t", str(target_secs + 3),
        output,
    ]

    r = run(cmd, check=False)
    if r.returncode == 0:
        return

    # Fallback: simple concat (no transitions)
    list_file = output.replace(".mp4", "_list.txt")
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")

    r2 = run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c:v", "libx264", "-crf", "23", "-preset", "slow",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output,
    ], check=False)
    os.remove(list_file)

    if r2.returncode != 0:
        raise RuntimeError(f"Falha ao montar vídeo final:\n{r2.stderr}\n\n(xfade error: {r.stderr})")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8765, reload=False)
