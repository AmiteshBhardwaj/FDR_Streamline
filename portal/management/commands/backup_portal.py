"""Consistent SQLite snapshot plus immutable encrypted evidence."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import zipfile

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone


class Command(BaseCommand):
    help = "Back up local SQLite and referenced encrypted documents; keys are excluded by default."

    def add_arguments(self, parser):
        parser.add_argument("--output", required=True, help="New ZIP archive path; existing files are never overwritten.")
        parser.add_argument("--include-dev-keys", action="store_true", help="Include effective keys for a disposable DEBUG development backup only.")

    def handle(self, *args, **options):
        if connection.vendor != "sqlite":
            raise CommandError("This command supports SQLite only. Use coordinated pg_dump and private-volume backups for PostgreSQL.")
        if connection.in_atomic_block:
            raise CommandError("Run backup outside an active database transaction.")
        if options["include_dev_keys"] and not settings.DEBUG:
            raise CommandError("Including keys is allowed only in DEBUG development mode.")
        output = Path(options["output"]).expanduser().absolute()
        if output.exists() or output.is_symlink():
            raise CommandError("The output path already exists; choose a new archive name.")
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        manifest = {"format": "fdr-streamline-backup", "version": 1, "created_at": timezone.now().isoformat(), "includes_development_keys": bool(options["include_dev_keys"]), "files": {}}
        output_created = False
        try:
            with tempfile.TemporaryDirectory(prefix="fdr-backup-") as staging:
                database = Path(staging) / "portal.sqlite3"
                connection.ensure_connection()
                with sqlite3.connect(database) as snapshot:
                    connection.connection.backup(snapshot)
                    references = [row[0] for row in snapshot.execute("SELECT encrypted_path FROM portal_document")]
                    if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise CommandError("The database snapshot failed its integrity check.")
                sources = {"portal.sqlite3": database}
                private_root = Path(settings.PRIVATE_STORAGE_ROOT).resolve()
                for relative in references:
                    logical = PurePosixPath(relative)
                    if logical.is_absolute() or ".." in logical.parts or "\\" in relative:
                        raise CommandError("A document contains an unsafe storage path.")
                    document = (private_root / relative).resolve()
                    if private_root not in document.parents or not document.is_file():
                        raise CommandError("A referenced encrypted document is missing or outside private storage; backup aborted.")
                    sources[f"documents/{logical.as_posix()}"] = document
                if options["include_dev_keys"]:
                    secrets_file = Path(staging) / "secrets.json"
                    secrets_file.write_text(json.dumps({"secret": settings.SECRET_KEY, "document_key": settings.DOCUMENT_ENCRYPTION_KEY}))
                    secrets_file.chmod(0o600)
                    sources["secrets.json"] = secrets_file
                marker = Path(settings.LOCAL_ROOT) / "demo-mode"
                if settings.DEBUG and marker.is_file():
                    sources["demo-mode"] = marker
                descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                output_created = True
                with os.fdopen(descriptor, "wb") as handle, zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name, source in sources.items():
                        digest = hashlib.sha256()
                        size = 0
                        with source.open("rb") as incoming, archive.open(name, "w") as outgoing:
                            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                                digest.update(chunk)
                                size += len(chunk)
                                outgoing.write(chunk)
                        manifest["files"][name] = {"sha256": digest.hexdigest(), "size": size}
                    archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        except Exception as error:
            if output_created:
                output.unlink(missing_ok=True)
            if isinstance(error, CommandError):
                raise
            raise CommandError(f"Backup failed ({type(error).__name__}); no completed archive was left.") from error
        self.stdout.write(self.style.SUCCESS(f"Backup created: {output} ({len(references)} encrypted documents)."))
        self.stdout.write("Keep the archive private: database metadata is confidential. Preserve the document encryption key separately." if not options["include_dev_keys"] else "Development keys are included. Treat this entire archive as a secret.")
