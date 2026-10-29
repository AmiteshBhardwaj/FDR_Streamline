import json
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from portal.monitoring import run_monitoring


class Command(BaseCommand):
    help = "Generate scoped deadline reminders and retry the durable email outbox."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one pass and exit (suitable for cron).")
        parser.add_argument("--interval", type=int, default=60, help="Seconds between passes; default: 60.")

    def handle(self, *args, **options):
        if options["interval"] < 1:
            raise CommandError("--interval must be at least one second.")
        try:
            while True:
                try:
                    result = run_monitoring()
                except Exception as error:
                    message = f"Monitoring failed ({type(error).__name__}); check database connectivity and worker status."
                    if options["once"]:
                        raise CommandError(message) from error
                    self.stderr.write(self.style.ERROR(message))
                else:
                    self.stdout.write(json.dumps(result, sort_keys=True))
                finally:
                    close_old_connections()
                if options["once"]:
                    break
                time.sleep(options["interval"])
        except KeyboardInterrupt:
            self.stdout.write("Deadline monitor stopped.")
