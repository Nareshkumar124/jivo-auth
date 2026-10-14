"""
Attachment checks and serving. Files are stored under MEDIA_ROOT with
random names (never served by the static file server) and streamed by the
portal only after the permission check in support.views.
"""

import mimetypes
import os
import re
import unicodedata

from django.core.exceptions import ValidationError


# What a download is labelled as. Anything else is served as a generic
# binary file, so a crafted upload can't be rendered by the browser.
SAFE_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/pdf",
    "text/plain",
    "text/csv",
    "application/json",
    "application/zip",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

# Image types shown inline in the ticket page, checked by their first bytes.
IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)

IMAGE_EXTENSIONS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}

# Never accepted, whatever the settings say.
BLOCKED_EXTENSIONS = {"exe", "bat", "cmd", "com", "msi", "scr", "ps1", "sh", "js", "jar", "vbs", "html", "htm", "svg", "php"}


def clean_filename(name):
    name = unicodedata.normalize("NFKC", os.path.basename(name or "")).strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)

    return name[:255] or "file"


def extension_of(name):
    return os.path.splitext(name)[1].lower().lstrip(".")


def sniff_image(uploaded):
    uploaded.seek(0)
    head = uploaded.read(16)
    uploaded.seek(0)

    for signature, content_type in IMAGE_SIGNATURES:
        if head.startswith(signature):
            if content_type == "image/webp" and head[8:12] != b"WEBP":
                continue
            return content_type

    return None


def validate_upload(uploaded, config):
    """
    Check one uploaded file against the support settings. Returns the
    content type to store, or raises ValidationError with a message that
    names the file.
    """

    name = clean_filename(uploaded.name)
    extension = extension_of(name)

    if uploaded.size == 0:
        raise ValidationError(f"{name} is empty.")

    if uploaded.size > config.max_attachment_bytes:
        raise ValidationError(
            f"{name} is larger than {config.max_attachment_mb} MB."
        )

    allowed = config.allowed_extension_set() - BLOCKED_EXTENSIONS

    if not extension or extension not in allowed:
        raise ValidationError(
            f"{name}: this file type isn't allowed. Allowed: "
            + ", ".join(sorted(allowed)) + "."
        )

    if extension in IMAGE_EXTENSIONS:
        sniffed = sniff_image(uploaded)

        if sniffed != IMAGE_EXTENSIONS[extension]:
            raise ValidationError(f"{name} isn't a valid {extension.upper()} image.")

        return sniffed

    guessed, _ = mimetypes.guess_type(name)

    return guessed or "application/octet-stream"


def download_content_type(attachment):
    content_type = attachment.content_type or ""

    return content_type if content_type in SAFE_CONTENT_TYPES else "application/octet-stream"


def human_size(size):
    size = float(size or 0)

    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}".replace(".0 ", " ")
        size /= 1024

    return f"{size:.1f} GB"
