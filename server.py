import asyncio
import re
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from lib import (
    IMAGE_GLOBS,
    Subject,
    archive_subject,
    find_subjects,
    get_subjects_dir,
    string_hash,
)

SUBJECTS_DIR = get_subjects_dir()
SUBJECTS_DIR.mkdir(exist_ok=True, parents=True)
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
SYNC_DEBOUNCE_SECONDS = 3
VIDEO_GLOBS = ("*.mp4", "*.webm", "*.mkv", "*.mov", "*.m4v", "*.avi", "*.flv")
# Sync never downloads gifs (see InvalidFormatException), but they're sometimes hand-collected format.
DISPLAY_IMAGE_GLOBS = (*IMAGE_GLOBS, "*.gif")
_CACHEABLE_SUFFIXES = {Path(g).suffix for g in (*DISPLAY_IMAGE_GLOBS, *VIDEO_GLOBS)}


class MediaFiles(StaticFiles):
    """StaticFiles, plus a far-future Cache-Control on image/video files - immutable once downloaded."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if Path(full_path).suffix in _CACHEABLE_SUFFIXES:
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response


app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/media", MediaFiles(directory=SUBJECTS_DIR), name="media")

# Debounce state lives in-process, so it only works with a single uvicorn worker. Keyed by subject dir name.
_pending_image_syncs: dict[str, asyncio.Task] = {}
_pending_video_syncs: dict[str, asyncio.Task] = {}
_subject_locks: dict[str, asyncio.Lock] = {}

# Index/gallery caches, keyed by subject dir name; a subject refreshes when its YAML mtime changes.
_subject_cache: dict[str, tuple[float, Subject]] = {}
_media_cache: dict[str, "_MediaCacheEntry"] = {}


class _MediaCacheEntry(BaseModel):
    yaml_mtime: float
    image_paths: list[tuple[Path, float]]  # (path, file mtime)
    video_paths: list[tuple[Path, float]]


def cached_subjects() -> list[Subject]:
    """find_subjects(), cached via `_subject_cache`."""
    return list(find_subjects(SUBJECTS_DIR, cache=_subject_cache))


def cached_media(subject: Subject) -> _MediaCacheEntry:
    """On-disk image/video paths and mtimes for `subject`, recomputed only when its YAML mtime changes."""
    name = subject.directory.name
    yaml_mtime = subject.updated_at.timestamp() if subject.updated_at else 0.0
    cached = _media_cache.get(name)
    if cached is not None and cached.yaml_mtime == yaml_mtime:
        return cached

    entry = _MediaCacheEntry(
        yaml_mtime=yaml_mtime,
        image_paths=sorted(
            (p, p.stat().st_mtime)
            for glob in DISPLAY_IMAGE_GLOBS
            for p in subject.directory.glob(glob)
        ),
        video_paths=sorted(
            (p, p.stat().st_mtime)
            for glob in VIDEO_GLOBS
            for p in subject.directory.glob(glob)
        ),
    )
    _media_cache[name] = entry
    return entry


def sanitize_subject_name(name: str) -> str:
    """Trim and strip characters that aren't valid in a filename, keeping the rest intact."""
    name = INVALID_FILENAME_CHARS.sub("", name.strip())
    return name.strip(" .")


def get_subject_or_404(name: str) -> Subject:
    for subject in find_subjects(SUBJECTS_DIR):
        if subject.directory.name == name:
            return subject
    raise HTTPException(status_code=404, detail=f"Subject {name!r} not found")


def build_media_entries(
    subject_urls: list[str],
    subject_dir: Path,
    request: Request,
    globs: tuple[str, ...],
) -> list[dict]:
    url_by_stem = {string_hash(url): url for url in subject_urls}
    return [
        {
            "filename": path.name,
            "media_url": str(
                request.url_for("media", path=f"{subject_dir.name}/{path.name}")
            ),
            "source_url": url_by_stem.get(path.stem),
        }
        for path in sorted(p for glob in globs for p in subject_dir.glob(glob))
    ]


GALLERY_PAGE_SIZE = 60


def gallery_entries(request: Request) -> list[dict]:
    """All images/videos across every subject, newest file (by mtime) first."""
    entries = []
    for subject in cached_subjects():
        subject_dir = subject.directory
        media = cached_media(subject)
        for kind, paths in (
            ("image", media.image_paths),
            ("video", media.video_paths),
        ):
            for path, mtime in paths:
                entries.append(
                    {
                        "kind": kind,
                        "filename": path.name,
                        "media_url": str(
                            request.url_for(
                                "media", path=f"{subject_dir.name}/{path.name}"
                            )
                        ),
                        "subject_name": subject.name,
                        "subject_url": str(
                            request.url_for("subject_detail", name=subject_dir.name)
                        ),
                        "mtime": mtime,
                    }
                )
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def pending_urls(
    subject_urls: list[str], subject_dir: Path, globs: tuple[str, ...]
) -> list[str]:
    """URLs with no corresponding file on disk yet - queued for background download."""
    downloaded_stems = {p.stem for glob in globs for p in subject_dir.glob(glob)}
    return [u for u in subject_urls if string_hash(u) not in downloaded_stems]


def _lock_for(name: str) -> asyncio.Lock:
    return _subject_locks.setdefault(name, asyncio.Lock())


def is_syncing(name: str) -> bool:
    """Whether a sync is currently running for this subject, as opposed to merely pending."""
    lock = _subject_locks.get(name)
    return lock.locked() if lock is not None else False


async def _debounced_sync(
    pending: dict[str, asyncio.Task],
    name: str,
    sync_method_name: str,
    subject_dir: Path,
) -> None:
    await asyncio.sleep(SYNC_DEBOUNCE_SECONDS)
    # Drop out of `pending` first: only a still-sleeping timer is cancellable, so a later
    # add can no longer cancel this run - it'll queue behind our lock instead.
    pending.pop(name, None)
    async with _lock_for(name):
        # Reloaded fresh, since this may run well after the request that queued it.
        subject = Subject.from_directory(subject_dir)
        await getattr(subject, sync_method_name)()
        subject.save()


def schedule_sync(
    pending: dict[str, asyncio.Task],
    sync_method_name: str,
    name: str,
    subject_dir: Path,
) -> None:
    """(Re)schedules a debounced sync, cancelling any not-yet-started one. sync_method_name names
    the Subject method to call, e.g. "sync_images"."""
    existing = pending.get(name)
    if existing is not None:
        existing.cancel()
    pending[name] = asyncio.create_task(
        _debounced_sync(pending, name, sync_method_name, subject_dir)
    )


@app.get("/")
def index(request: Request):
    subjects = []
    for subject in cached_subjects():
        media = cached_media(subject)
        subjects.append(
            {
                "name": subject.name,
                "dir_name": subject.directory.name,
                "image_count": len(media.image_paths),
                "video_count": len(media.video_paths),
                "reference_count": len(subject.references),
                "created_at": subject.created_at,
                "updated_at": subject.updated_at,
            }
        )
    subjects.sort(key=lambda s: s["name"].lower())
    return templates.TemplateResponse(request, "index.html", {"subjects": subjects})


@app.get("/gallery")
def gallery(request: Request, page: int = 1):
    entries = gallery_entries(request)
    total_pages = max(1, -(-len(entries) // GALLERY_PAGE_SIZE))
    page = min(max(page, 1), total_pages)
    start = (page - 1) * GALLERY_PAGE_SIZE
    return templates.TemplateResponse(
        request,
        "gallery.html",
        {
            "entries": entries[start : start + GALLERY_PAGE_SIZE],
            "page": page,
            "total_pages": total_pages,
        },
    )


@app.post("/subjects/add")
def add_subject(name: str = Form(...)):
    sanitized = sanitize_subject_name(name)
    if not sanitized:
        raise HTTPException(status_code=400, detail="Subject name cannot be empty")
    subject_dir = SUBJECTS_DIR / sanitized
    if subject_dir.exists():
        raise HTTPException(
            status_code=400, detail=f"Subject {sanitized!r} already exists"
        )
    subject_dir.mkdir(parents=True)
    Subject(name=sanitized, directory=subject_dir).save()
    return RedirectResponse(url=f"/subjects/{sanitized}", status_code=303)


@app.get("/subjects/{name}")
def subject_detail(request: Request, name: str):
    subject = get_subject_or_404(name)
    subject_dir = subject.directory
    image_entries = build_media_entries(
        subject.images, subject_dir, request, DISPLAY_IMAGE_GLOBS
    )
    video_entries = build_media_entries(
        subject.videos, subject_dir, request, VIDEO_GLOBS
    )
    return templates.TemplateResponse(
        request,
        "subject.html",
        {
            "subject": subject,
            "subject_dir_name": subject_dir.name,
            "image_entries": image_entries,
            "video_entries": video_entries,
            "pending_images": pending_urls(subject.images, subject_dir, IMAGE_GLOBS),
            "pending_videos": pending_urls(subject.videos, subject_dir, VIDEO_GLOBS),
            "running": is_syncing(subject_dir.name),
        },
    )


@app.get("/subjects/{name}/status")
def subject_status(name: str):
    """Lightweight JSON endpoint for the subject page's auto-refresh polling."""
    subject = get_subject_or_404(name)
    subject_dir = subject.directory
    pending = len(pending_urls(subject.images, subject_dir, IMAGE_GLOBS)) + len(
        pending_urls(subject.videos, subject_dir, VIDEO_GLOBS)
    )
    return {"pending": pending, "running": is_syncing(subject_dir.name)}


@app.post("/subjects/{name}/body")
async def update_body(name: str, body: str = Form("")):
    async with _lock_for(name):
        subject = get_subject_or_404(name)
        subject.body = body
        subject.save()
    return RedirectResponse(url=f"/subjects/{name}", status_code=303)


@app.post("/subjects/{name}/references/add")
async def add_reference(name: str, url: str = Form(...)):
    url = url.strip()
    if url:
        async with _lock_for(name):
            subject = get_subject_or_404(name)
            subject.references = sorted(set(subject.references) | {url})
            subject.save()
    return RedirectResponse(url=f"/subjects/{name}", status_code=303)


@app.post("/subjects/{name}/references/remove")
async def remove_reference(name: str, url: str = Form(...)):
    async with _lock_for(name):
        subject = get_subject_or_404(name)
        subject.references = [r for r in subject.references if r != url]
        subject.save()
    return RedirectResponse(url=f"/subjects/{name}", status_code=303)


@app.post("/subjects/{name}/images/add")
async def add_image(name: str, url: str = Form(...)):
    url = url.strip()
    if url:
        async with _lock_for(name):
            subject = get_subject_or_404(name)
            subject.images = sorted(set(subject.images) | {url})
            subject.save()
        schedule_sync(_pending_image_syncs, "sync_images", name, subject.directory)
    return RedirectResponse(url=f"/subjects/{name}", status_code=303)


@app.post("/subjects/{name}/images/remove")
async def remove_image(name: str, url: str = Form(...)):
    async with _lock_for(name):
        subject = get_subject_or_404(name)
        subject.images = [u for u in subject.images if u != url]
        for fmt in ("jpg", "png"):
            (subject.directory / f"{string_hash(url)}.{fmt}").unlink(missing_ok=True)
        subject.save()
    return RedirectResponse(url=f"/subjects/{name}", status_code=303)


@app.post("/subjects/{name}/videos/add")
async def add_video(name: str, url: str = Form(...)):
    url = url.strip()
    if url:
        async with _lock_for(name):
            subject = get_subject_or_404(name)
            subject.videos = sorted(set(subject.videos) | {url})
            subject.save()
        schedule_sync(_pending_video_syncs, "sync_videos", name, subject.directory)
    return RedirectResponse(url=f"/subjects/{name}", status_code=303)


@app.post("/subjects/{name}/videos/remove")
async def remove_video(name: str, url: str = Form(...)):
    async with _lock_for(name):
        subject = get_subject_or_404(name)
        subject.videos = [v for v in subject.videos if v != url]
        for path in subject.directory.glob(f"{string_hash(url)}.*"):
            path.unlink(missing_ok=True)
        subject.save()
    return RedirectResponse(url=f"/subjects/{name}", status_code=303)


@app.post("/subjects/{name}/archive")
async def archive_subject_route(name: str):
    """Zips the subject's directory and removes it, so it stops appearing in the UI. The
    zip is left alongside the subjects directory - restore by unzipping it back in place."""
    for pending in (_pending_image_syncs, _pending_video_syncs):
        task = pending.pop(name, None)
        if task is not None:
            task.cancel()
    async with _lock_for(name):
        subject = get_subject_or_404(name)
        try:
            archive_subject(subject)
        except FileExistsError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        _media_cache.pop(name, None)
    return RedirectResponse(url="/", status_code=303)


@app.post("/subjects/{name}/files/remove")
async def remove_orphan_file(name: str, filename: str = Form(...)):
    """Deletes an orphaned media file (no URL in subject.images/videos) straight off disk."""
    async with _lock_for(name):
        subject = get_subject_or_404(name)
        path = subject.directory / filename
        if (
            Path(filename).name != filename
            or not any(path.match(g) for g in (*DISPLAY_IMAGE_GLOBS, *VIDEO_GLOBS))
            or not path.is_file()
        ):
            raise HTTPException(status_code=400, detail="Invalid file")
        path.unlink()
    return RedirectResponse(url=f"/subjects/{name}", status_code=303)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
