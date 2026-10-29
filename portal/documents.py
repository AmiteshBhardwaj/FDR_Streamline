"""Private, authenticated encryption for immutable PDF and image evidence."""
import base64
import hashlib
import io
import os
from pathlib import Path
import warnings

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import transaction
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .access import can_access, profile_for
from .models import Document, Instrument, ReleaseRequest, Role
from .services import _cas, _fresh_instrument, audit


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


def _cipher():
    try:
        key = base64.urlsafe_b64decode(settings.DOCUMENT_ENCRYPTION_KEY)
        if len(key) != 32:
            raise ValueError("Key must contain 32 bytes")
        return AESGCM(key)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ImproperlyConfigured("DOCUMENT_ENCRYPTION_KEY must be a base64-encoded 32-byte key.") from exc


def _validate_content(uploaded_file):
    if getattr(uploaded_file, "size", 0) > MAX_DOCUMENT_BYTES:
        raise ValidationError("Documents must be 10 MB or smaller.")
    chunks, length = [], 0
    for chunk in uploaded_file.chunks():
        length += len(chunk)
        if length > MAX_DOCUMENT_BYTES:
            raise ValidationError("Documents must be 10 MB or smaller.")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise ValidationError("Empty documents cannot be uploaded.")
    if content.startswith(b"%PDF-"):
        try:
            document = PdfReader(io.BytesIO(content), strict=True)
            if document.is_encrypted or len(document.pages) < 1 or len(document.pages) > 500:
                raise ValidationError("Use an unencrypted PDF with 1–500 pages.")
            # PDFs are downloaded as attachments, never embedded as executable content.
            return content, "application/pdf"
        except (PdfReadError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise ValidationError("The PDF is damaged or cannot be read.") from exc
    if not (content.startswith(b"\x89PNG\r\n\x1a\n") or content.startswith(b"\xff\xd8\xff")):
        raise ValidationError("Upload a valid PDF, PNG, or JPEG document.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in ["PNG", "JPEG"] or image.width * image.height > 25_000_000:
                    raise ValidationError("Use a PNG or JPEG image no larger than 25 megapixels.")
                mime_type = "image/png" if image.format == "PNG" else "image/jpeg"
                image.verify()
        return content, mime_type
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError("The image is damaged or exceeds safe dimensions.") from exc


def _upload_allowed(actor, instrument, category):
    profile = profile_for(actor)
    if not can_access(actor, instrument):
        raise PermissionDenied("This document is outside your institution scope.")
    if category not in Document.Category.values:
        raise ValidationError("Choose a valid document category.")
    if profile.role == Role.INSTITUTION:
        if category in ["instrument", "supporting"] and instrument.status in ["draft", "corrections"]:
            return
        if category == "release" and instrument.status == "accepted":
            if not instrument.release_requests.filter(status__in=["recommended", "approved"]).exists():
                return
    if profile.role == Role.REVIEWER:
        if category == "verification" and instrument.status in ["submitted", "review", "corrections", "accepted"]:
            return
        if category == "completion" and instrument.release_requests.filter(status="approved").exists():
            return
    raise PermissionDenied("Your role cannot upload this document category at the current workflow stage.")


def upload_document(actor, instrument, uploaded_file, category):
    # Validate authorization before reading or parsing an untrusted file.
    _upload_allowed(actor, instrument, category)
    content, mime_type = _validate_content(uploaded_file)
    cipher = _cipher()
    document = Document(instrument=instrument, category=category,
                        original_name=Path(uploaded_file.name.replace("\\", "/")).name[:255],
                        mime_type=mime_type, size=len(content), checksum=hashlib.sha256(content).hexdigest(), uploaded_by=actor)
    root = Path(settings.PRIVATE_STORAGE_ROOT).resolve()
    relative_path = Path(str(instrument.pk)) / f"{document.pk}.enc"
    path = root / relative_path
    nonce = os.urandom(12)
    encrypted = nonce + cipher.encrypt(nonce, content, document.pk.bytes)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stored:
            stored.write(encrypted)
        with transaction.atomic():
            instrument = _fresh_instrument(instrument)
            _upload_allowed(actor, instrument, category)
            changes = {}
            if category in ["instrument", "supporting"] and instrument.bank_status == "confirmed":
                changes["bank_status"] = "unconfirmed"
            _cas(instrument, instrument.version, **changes)
            document.instrument = instrument
            document.encrypted_path = str(relative_path)
            document.full_clean()
            document.save()
            audit(actor, instrument, "document.uploaded", document_id=str(document.pk), category=category, checksum=document.checksum, size=document.size)
        return document
    except Exception:
        # Only the newly created orphan is removed; accepted records are immutable.
        path.unlink(missing_ok=True)
        raise


def read_document(document):
    """Read after the caller authorizes the user against document.instrument."""
    root = Path(settings.PRIVATE_STORAGE_ROOT).resolve()
    path = (root / document.encrypted_path).resolve()
    if root not in path.parents:
        raise ValidationError("The document storage path is invalid.")
    try:
        encrypted = path.read_bytes()
        content = _cipher().decrypt(encrypted[:12], encrypted[12:], document.pk.bytes)
    except (OSError, InvalidTag, ValueError) as exc:
        raise ValidationError("This document is unavailable or failed its integrity check.") from exc
    if len(content) != document.size or hashlib.sha256(content).hexdigest() != document.checksum:
        raise ValidationError("The document failed its integrity check.")
    return content
