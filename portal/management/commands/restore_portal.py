"""Validate and restore into a separate directory, never the live database."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tempfile
import zipfile

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Verify a backup and restore into a new/empty separate directory without changing the running portal."

    def add_arguments(self, parser):
        parser.add_argument("archive", help="ZIP archive produced by backup_portal.")
        parser.add_argument("--destination", required=True, help="New or empty restore directory, separate from the live database and storage.")

    def handle(self, *args, **options):
        requested = Path(options["destination"]).expanduser().absolute()
        if requested.is_symlink():
            raise CommandError("The restore destination cannot be a symbolic link.")
        destination = requested.resolve()
        live_db = Path(str(settings.DATABASES["default"]["NAME"])).resolve()
        live_storage = Path(settings.PRIVATE_STORAGE_ROOT).resolve()
        if destination in (live_db, live_db.parent, live_storage) or destination in live_storage.parents or live_storage in destination.parents:
            raise CommandError("Choose a destination separate from the current database and document storage.")
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise CommandError("The restore destination must be new or empty; existing data is never overwritten.")
        try:
            with zipfile.ZipFile(options["archive"], "r") as archive:
                members = archive.infolist()
                names = [member.filename for member in members]
                if len(names) != len(set(names)) or "manifest.json" not in names:
                    raise CommandError("The archive has duplicate members or no manifest.")
                if len(names) > 100000 or sum(member.file_size for member in members) > 10 * 1024 ** 3:
                    raise CommandError("This restore exceeds the local backup safety limits (100,000 files / 10 GiB).")
                for member in members:
                    path = PurePosixPath(member.filename)
                    mode = member.external_attr >> 16
                    if path.is_absolute() or ".." in path.parts or "\\" in member.filename or str(path) != member.filename or member.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                        raise CommandError("The archive contains an unsafe path or unsupported file type.")
                if archive.getinfo("manifest.json").file_size > 20 * 1024 * 1024:
                    raise CommandError("The backup manifest is too large.")
                manifest = json.loads(archive.read("manifest.json"))
                files = manifest.get("files", {})
                if manifest.get("format") != "fdr-streamline-backup" or manifest.get("version") != 1 or not isinstance(files, dict) or set(files) != set(names) - {"manifest.json"} or "portal.sqlite3" not in files:
                    raise CommandError("The archive manifest or file inventory is invalid.")
                if any(name not in ("portal.sqlite3", "secrets.json", "demo-mode") and not name.startswith("documents/") for name in files):
                    raise CommandError("The archive contains unsupported files.")
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".fdr-restore-", dir=destination.parent) as staging:
                    restored = Path(staging) / "restored"
                    restored.mkdir(mode=0o700)
                    for name, expected in files.items():
                        if not isinstance(expected, dict) or archive.getinfo(name).file_size != expected.get("size"):
                            raise CommandError("An archive member does not match its manifest size.")
                        target = restored / name
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        digest = hashlib.sha256()
                        with archive.open(name) as incoming, target.open("xb") as outgoing:
                            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                                digest.update(chunk)
                                outgoing.write(chunk)
                        target.chmod(0o600)
                        if digest.hexdigest() != expected.get("sha256"):
                            raise CommandError("An archive member failed its SHA-256 integrity check.")
                    with sqlite3.connect(f"file:{restored / 'portal.sqlite3'}?mode=ro", uri=True) as database:
                        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise CommandError("The restored database failed its integrity check.")
                        for (relative,) in database.execute("SELECT encrypted_path FROM portal_document"):
                            logical = PurePosixPath(relative)
                            if logical.is_absolute() or ".." in logical.parts or "\\" in relative or f"documents/{relative}" not in files:
                                raise CommandError("The restored database references an unsafe or missing document.")
                    (restored / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
                    (restored / "manifest.json").chmod(0o600)
                    os.replace(restored, destination)
        except Exception as error:
            if isinstance(error, CommandError):
                raise
            raise CommandError(f"Restore failed ({type(error).__name__}); the current portal was not modified.") from error
        self.stdout.write(self.style.SUCCESS(f"Verified restore written to {destination}. The current portal remains unchanged."))
        self.stdout.write("To use it after stopping the portal, set SQLITE_PATH and PRIVATE_STORAGE_ROOT to the restored paths and provide the original DOCUMENT_ENCRYPTION_KEY. Test access before switching traffic.")
