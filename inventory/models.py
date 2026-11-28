from django.conf import settings
from django.db import models


class Vendor(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="vendor_profile",
        null=True,
        blank=True,
    )
    full_name = models.CharField(max_length=200)
    shop_name = models.CharField(max_length=200, unique=True)
    email = models.EmailField(unique=True)
    shop_address = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["shop_name"]

    def __str__(self):
        return self.shop_name
