import hashlib
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db.models import Prefetch, Q

from .models import Category, Product, ProductReview


def invalidate_catalog_cache():
    cache.set("storefront:catalog:version", uuid.uuid4().hex, timeout=None)


def _catalog_cache_key(resource, vendor_id=None, category_slug=None, query=None):
    version = cache.get_or_set("storefront:catalog:version", "1", timeout=None)
    identity = "\0".join(
        (
            str(vendor_id or ""),
            category_slug or "",
            query or "",
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"storefront:{resource}:{version}:{digest}"


def get_catalog_products(vendor_id=None, category_slug=None, query=None):
    key = _catalog_cache_key("products", vendor_id, category_slug, query)

    def load_products():
        products = (
            Product.objects.select_related("category__parent__parent")
            .prefetch_related(
                Prefetch(
                    "reviews",
                    queryset=ProductReview.objects.order_by("-created_at"),
                )
            )
        )
        if vendor_id is not None:
            products = products.filter(vendor_id=vendor_id)
        if category_slug:
            products = products.filter(category__slug=category_slug)
        if query:
            products = products.filter(
                Q(name__icontains=query)
                | Q(category__name__icontains=query)
                | Q(category__parent__name__icontains=query)
                | Q(category__parent__parent__name__icontains=query)
            ).distinct()
        return list(products)

    return cache.get_or_set(
        key,
        load_products,
        timeout=getattr(settings, "CATALOG_CACHE_TIMEOUT", 60),
    )


def get_catalog_categories(vendor_id=None):
    key = _catalog_cache_key("categories", vendor_id)

    def load_categories():
        categories = Category.objects.filter(products__isnull=False)
        if vendor_id is not None:
            categories = categories.filter(products__vendor_id=vendor_id)
        return list(
            categories.select_related("parent__parent").distinct().order_by("slug")
        )

    return cache.get_or_set(
        key,
        load_categories,
        timeout=getattr(settings, "CATALOG_CACHE_TIMEOUT", 60),
    )
