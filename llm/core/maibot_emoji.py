"""WeChat compatibility fields resolve exclusively against MaiBot's catalog.

No directory scan, synonym parser, independent registry, or image conversion.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path


def _catalog_rows():
    from sqlmodel import select
    from src.common.database.database import get_db_session
    from src.common.database.database_model import Images, ImageType
    with get_db_session() as session:
        return session.exec(select(Images).filter_by(
            image_type=ImageType.EMOJI, is_registered=True, is_banned=False, no_file_flag=False,
        ).order_by(Images.id)).all()


def _available_path(record):
    from src.common.utils.image_path import resolve_stored_image_path
    try:
        path = resolve_stored_image_path(record.full_path)
        return path if path.is_file() else None
    except ValueError:
        return None


def native_emoji_catalog():
    """Return current registered hash -> absolute path, including WebUI changes."""
    return {row.image_hash: str(path) for row in _catalog_rows() if (path := _available_path(row))}


def get_native_emoji_path(reference):
    """Accept a native hash or an unambiguous exact native filename/stem.

    Paths and old synonym tokens cannot select unregistered or banned files.
    """
    if not isinstance(reference, str) or not reference.strip():
        return None
    reference = reference.strip()
    rows = _catalog_rows()
    matches = [row for row in rows if row.image_hash == reference]
    if not matches:
        matches = [row for row in rows if reference in {Path(row.full_path).name, Path(row.full_path).stem}]
    available = {row.image_hash: str(path) for row in matches if (path := _available_path(row))}
    return next(iter(available.values())) if len(available) == 1 else None


async def native_emoji_file(component):
    """Use MaiBot's persisted image, preserving GIF frames and original bytes."""
    from sqlmodel import select
    from src.common.database.database import get_db_session
    from src.common.database.database_model import Images, ImageType
    from src.common.utils.image_path import resolve_stored_image_path
    from src.emoji_system.emoji_manager import emoji_manager

    if not component.binary_data:
        await component.load_emoji_binary()
    data = component.binary_data
    if not data:
        raise ValueError('Empty outbound emoji')
    digest = hashlib.sha256(data).hexdigest()
    if component.binary_hash != digest:
        raise ValueError('Outbound emoji hash does not match its bytes')
    def verify_image():
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            image.verify()
    await asyncio.to_thread(verify_image)

    def is_banned():
        with get_db_session() as session:
            return session.exec(select(Images.id).filter_by(
                image_hash=digest, image_type=ImageType.EMOJI, is_banned=True,
            )).first() is not None
    if await asyncio.to_thread(is_banned):
        raise ValueError('Outbound emoji is banned in MaiBot')
    emoji = await emoji_manager.ensure_emoji_saved(data, emoji_hash=digest)
    path = resolve_stored_image_path(emoji.full_path)
    persisted = await asyncio.to_thread(path.read_bytes)
    if hashlib.sha256(persisted).hexdigest() != digest:
        raise ValueError('Stored MaiBot emoji bytes do not match the outgoing emoji')
    return path
