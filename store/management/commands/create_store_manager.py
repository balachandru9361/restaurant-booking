from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

from store.permissions import STORE_GROUP


class Command(BaseCommand):
    help = "Create (or update) a Store Manager login: manage.py create_store_manager <username> --password <pw>"

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--password", required=True)

    def handle(self, *args, **opts):
        User = get_user_model()
        group, _ = Group.objects.get_or_create(name=STORE_GROUP)
        user, created = User.objects.get_or_create(username=opts["username"])
        user.set_password(opts["password"])
        user.is_active = True
        user.save()
        user.groups.add(group)
        self.stdout.write(self.style.SUCCESS(
            f"Store manager '{user.username}' {'created' if created else 'updated'}. Login at /store/login/"
        ))
