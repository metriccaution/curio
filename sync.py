import asyncio

from lib import find_subjects, get_subjects_dir


async def main() -> None:
    for subject in find_subjects(get_subjects_dir()):
        await subject.sync_images()
        await subject.sync_videos()
        subject.save()


if __name__ == "__main__":
    asyncio.run(main())
