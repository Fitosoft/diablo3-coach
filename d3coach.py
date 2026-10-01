from pathlib import Path


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def claim_next(incoming: Path, processing: Path) -> Path | None:
    processing.mkdir(parents=True, exist_ok=True)
    images = sorted(
        path for path in incoming.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES
    )
    return images[0].replace(processing / images[0].name) if images else None
