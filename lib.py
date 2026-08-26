import hashlib
import os
import shutil
import tempfile
import zipfile
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from time import sleep
from typing import Literal

import imagehash
import pillow_avif  # noqa: F401
import requests
import yt_dlp
from PIL import Image
from pydantic import BaseModel, Field, model_validator
from yaml import SafeLoader, dump, load

IMAGE_GLOBS = ("*.jpg", "*.png")


def get_subjects_dir() -> Path:
    """Subjects directory: the SUBJECTS_DIR env var, or "subjects" if unset."""
    return Path(os.environ.get("SUBJECTS_DIR", "subjects"))


class InvalidFormatException(Exception):
    pass


class Subject(BaseModel):
    """A subject: images, videos, and reference URLs to collect."""

    name: str
    directory: Path = Field(exclude=True)
    body: str = ""
    image_format: Literal["jpg", "png"] = "jpg"
    references: list[str] = []
    images: list[str] = []
    videos: list[str] = []

    @model_validator(mode="after")
    def unique_references(self) -> "Subject":
        self.references = sorted(set(self.references))
        self.images = sorted(set(self.images))
        self.videos = sorted(set(self.videos))
        return self

    @staticmethod
    def _find_yaml_file(directory: Path) -> Path | None:
        """The directory's single YAML file, or None if it has none. Raises if more than one."""
        existing = list(directory.glob("*.yaml"))
        if len(existing) > 1:
            raise ValueError(
                f"Multiple YAML files in {directory}, refusing to guess which to use: "
                f"{', '.join(sorted(f.name for f in existing))}"
            )
        return existing[0] if existing else None

    @staticmethod
    def from_directory(subject_dir: Path) -> "Subject":
        """Loads the Subject from a directory's single YAML file. Raises if there isn't exactly one."""
        yaml_file = Subject._find_yaml_file(subject_dir)
        if yaml_file is None:
            raise ValueError(
                f"Expected exactly one YAML file in {subject_dir}, found 0"
            )
        with open(yaml_file) as f:
            return Subject(directory=subject_dir, **load(f, Loader=SafeLoader))

    @property
    def created_at(self) -> datetime | None:
        """Creation time of the YAML file, or None if unsaved. On Linux this is ctime, not true birth time."""
        path = self._find_yaml_file(self.directory)
        return datetime.fromtimestamp(path.stat().st_ctime, tz=UTC) if path else None

    @property
    def updated_at(self) -> datetime | None:
        """Last-modified time of the YAML file, or None if it hasn't been saved yet."""
        path = self._find_yaml_file(self.directory)
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC) if path else None

    def save(self) -> None:
        """Writes fields to the subject's YAML file (creating subject.yaml if needed); no-ops if unchanged."""
        target = self._find_yaml_file(self.directory) or Path(
            self.directory, "subject.yaml"
        )
        content = dump(
            self.model_dump(
                exclude_none=True, exclude_unset=True, exclude_defaults=True
            ),
            sort_keys=False,
        )
        if target.exists() and target.read_text() == content:
            return
        target.write_text(content)

    async def sync_images(self) -> None:
        """Downloads pending image URLs, dedupes on disk, and updates self.images to match what's kept."""
        self.images = await download_images(
            self.directory, self.images, [self.image_format]
        )
        pre_dedupe_stems = {
            p.stem for glob in IMAGE_GLOBS for p in self.directory.glob(glob)
        }
        kept_stems = await dedupe_directory(self.directory)
        removed_by_dedupe = pre_dedupe_stems - kept_stems
        self.images = [
            u for u in self.images if string_hash(u) not in removed_by_dedupe
        ]

    async def sync_videos(self) -> None:
        """Downloads every pending video URL, updating self.videos in place."""
        self.videos = await video_downloader(self.directory, self.videos)


def find_subjects(subject_dir: Path) -> Generator[Subject, None, None]:
    """Yields each subdirectory's Subject; scaffolds subject.yaml if missing, skips ambiguous ones."""

    for subdir in subject_dir.iterdir():
        if not subdir.is_dir():
            continue

        if not any(subdir.glob("*.yaml")):
            new_subject = Subject(name=subdir.stem, directory=subdir)
            new_subject.save()
            yield new_subject
            continue

        try:
            yield Subject.from_directory(subdir)
        except ValueError as e:
            print(f"{e}, skipping")


def archive_subject(subject: Subject) -> Path:
    """Zips the subject's directory into a sibling `<dir_name>.zip` and deletes the directory.

    The zip's entries are rooted at the directory name, so unzipping it back into the subjects
    directory restores the subject exactly (and it'll be picked up by find_subjects again)."""
    zip_path = subject.directory.parent / f"{subject.directory.name}.zip"
    if zip_path.exists():
        raise FileExistsError(f"Archive already exists at {zip_path}")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(subject.directory.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(subject.directory.parent))

    shutil.rmtree(subject.directory)
    return zip_path


def string_hash(to_hash: str) -> str:
    return hashlib.sha256(to_hash.encode("utf-8")).hexdigest()


async def download_images(
    download_dir: Path,
    urls: list[str],
    formats: list[str],
) -> list[str]:
    """Downloads urls into download_dir, skipping ones already saved. Returns all urls, including failed ones."""
    download_dir.mkdir(parents=True, exist_ok=True)

    existing_files = {
        image_path.stem
        for glob in IMAGE_GLOBS
        for image_path in download_dir.glob(glob)
    }

    already_downloaded = list(
        {i.strip() for i in urls if string_hash(i.strip()) in existing_files}
    )

    urls = list(
        {
            i.strip()
            for i in urls
            if len(i.strip()) > 0 and i.strip() not in already_downloaded
        }
    )
    urls.sort()

    for url in urls:
        try:
            print("Downloading", url)
            with requests.get(  # noqa: ASYNC210 - async is for signature compat, not concurrency
                url,
                stream=True,
                headers={
                    "Accept": "image/avif,image/webp,image/png,image/svg+xml,image/*;q=0.8,*/*;q=0.5",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:146.0) Gecko/20100101 Firefox/146.0",
                    "Accept-Encoding": "gzip, deflate, br",
                },
            ) as r:
                r.raise_for_status()
                with tempfile.TemporaryFile(prefix="img-dl") as fp:
                    for chunk in r.iter_content(chunk_size=8192):
                        fp.write(chunk)

                    with Image.open(fp) as image:
                        if image.format == "GIF":
                            raise InvalidFormatException(
                                "Gifs aren't allowed for download"
                            )

                        for format in formats:
                            image_path = Path(
                                download_dir, f"{string_hash(url)}.{format}"
                            )
                            format_image = (
                                image.convert("RGB")
                                if format == "jpg"
                                else image.copy()
                            )
                            format_image.thumbnail(
                                (2000, 2000), Image.Resampling.LANCZOS
                            )
                            format_image.save(image_path)
        except Exception as e:  # noqa: BLE001 - any failure here must retry next run, not crash the sync
            print(f"Failed to download {url}")
            print(repr(e))

    return sorted(urls + already_downloaded)


async def dedupe_directory(directory: Path) -> set[str]:
    """Deletes lower-resolution duplicate images in directory. Returns the filename stems of the ones kept."""
    best_by_hash: dict[imagehash.ImageHash, tuple[Path, int]] = {}

    image_paths = sorted(
        image_path for glob in IMAGE_GLOBS for image_path in directory.glob(glob)
    )

    for image_path in image_paths:
        with Image.open(image_path) as im:
            image_hash = imagehash.average_hash(im)
            area = im.width * im.height

        best = best_by_hash.get(image_hash)
        if best is None or area > best[1]:
            if best is not None:
                print(f"Removing duplicate {best[0].name} (kept {image_path.name})")
                best[0].unlink(missing_ok=True)
            best_by_hash[image_hash] = (image_path, area)
        else:
            print(f"Removing duplicate {image_path.name} (kept {best[0].name})")
            image_path.unlink(missing_ok=True)

    return {path.stem for path, _ in best_by_hash.values()}


async def video_downloader(download_dir: Path, urls: list[str]) -> list[str]:
    url_list = sorted(set(urls))

    first = True
    for url in url_list:
        stem = string_hash(url)
        if any(download_dir.glob(f"{stem}.*")):
            continue

        if not first:
            sleep(1)  # noqa: ASYNC251 - async is for signature compat, not concurrency

        # %(ext)s lets yt-dlp pick the real container; a literal ".mp4" gets the true
        # extension appended instead (e.g. "<stem>.mp4.webm"), breaking stem lookups.
        outtmpl = str(Path(download_dir, f"{stem}.%(ext)s"))
        with yt_dlp.YoutubeDL({"outtmpl": outtmpl}) as ydl:
            error_code = ydl.download(url)
            print(error_code)

        first = False

    return url_list
