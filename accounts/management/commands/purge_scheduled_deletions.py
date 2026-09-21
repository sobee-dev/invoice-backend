from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import AccountStatus, AccountStatusAudit, User
from accounts.services import purge_account


class Command(BaseCommand):
    help = "Hard-deletes accounts whose 1-year deletion grace period has expired."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help="List what would be deleted, delete nothing.")

    def handle(self, *args, **options):
        due = User.objects.filter(
            status=AccountStatus.PENDING_DELETION,
            deletion_scheduled_for__lte=timezone.now(),
        )
        count = due.count()
        self.stdout.write(f"{count} account(s) due for permanent deletion.")

        if options['dry_run']:
            for user in due:
                self.stdout.write(f"  would delete: {user.email} (scheduled {user.deletion_scheduled_for})")
            return

        for user in due:
            with transaction.atomic():
                AccountStatusAudit.objects.create(
                    user_id=user.id, email=user.email,
                    from_status=AccountStatus.PENDING_DELETION, to_status=AccountStatus.DELETED,
                    reason='Automatic purge — 1-year grace period elapsed.',
                )
                purge_account(user)

        self.stdout.write(self.style.SUCCESS(f"Purged {count} account(s)."))