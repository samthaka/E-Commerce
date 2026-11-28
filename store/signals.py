from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .cache import invalidate_catalog_cache
from .models import Category, Product, ProductReview


@receiver((post_save, post_delete), sender=Product)
@receiver((post_save, post_delete), sender=Category)
@receiver((post_save, post_delete), sender=ProductReview)
def invalidate_catalog_on_change(sender, **kwargs):
    transaction.on_commit(invalidate_catalog_cache)
