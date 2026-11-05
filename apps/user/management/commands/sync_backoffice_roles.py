from django.core.management.base import BaseCommand
from apps.user.roles import sync_backoffice_roles


class Command(BaseCommand):
    help = "Idempotently synchronize initial Back Office role groups and permissions."

    def handle(self, *args, **options):
        self.stdout.write("Synchronizing Back Office role groups and permissions...")
        groups = sync_backoffice_roles()
        for name, group in groups.items():
            perm_count = group.permissions.count()
            self.stdout.write(self.style.SUCCESS(f"  - {name}: {perm_count} permissions attached"))
        self.stdout.write(self.style.SUCCESS("Synchronization complete."))
