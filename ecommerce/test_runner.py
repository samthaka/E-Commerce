from django.conf import settings
from django.core.management.base import CommandError
from django.test.runner import DiscoverRunner


class TestEnvironmentRunner(DiscoverRunner):
    def run_tests(self, test_labels, extra_tests=None, **kwargs):
        if settings.ENV_CONFIG != "test":
            raise CommandError("Tests can only run when ENV_CONFIG=test.")
        return super().run_tests(test_labels, extra_tests, **kwargs)
