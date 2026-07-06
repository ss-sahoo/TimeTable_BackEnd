"""
Management command to activate an institute by subdomain.
Usage:
    python manage.py activate_institute diracai
"""
from django.core.management.base import BaseCommand, CommandError
from accounts.models import Institute


class Command(BaseCommand):
    help = 'Activate an institute by subdomain (sets is_active=True)'

    def add_arguments(self, parser):
        parser.add_argument(
            'subdomain',
            type=str,
            help='Subdomain slug of the institute to activate',
        )

    def handle(self, *args, **options):
        subdomain = options['subdomain'].strip().lower()
        try:
            institute = Institute.objects.using('default').get(subdomain__iexact=subdomain)
        except Institute.DoesNotExist:
            raise CommandError(f"No institute found with subdomain: '{subdomain}'")

        old_status = institute.is_active
        institute.is_active = True
        institute.save(using='default')

        self.stdout.write(
            self.style.SUCCESS(
                f"✅ Institute '{institute.name}' (ID: {institute.id}) "
                f"is_active: {old_status} → True"
            )
        )
