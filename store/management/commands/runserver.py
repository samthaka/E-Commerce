from django.conf import settings
from django.core.management.base import CommandError
from django.contrib.staticfiles.management.commands.runserver import (
    Command as RunserverCommand,
)


class Command(RunserverCommand):
    def handle(self, *args, **options):
        if settings.ENV_CONFIG == "test":
            raise CommandError("The development server cannot run when ENV_CONFIG=test.")
        return super().handle(*args, **options)
