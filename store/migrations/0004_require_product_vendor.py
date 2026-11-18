from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


def assign_legacy_products_to_vendors(apps, schema_editor):
    Product = apps.get_model("store", "Product")
    Vendor = apps.get_model("inventory", "Vendor")
    database = schema_editor.connection.alias

    vendor_ids = list(
        Vendor.objects.using(database)
        .filter(Q(user__isnull=True) | Q(user__is_staff=False))
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if not vendor_ids and Product.objects.using(database).filter(vendor__isnull=True).exists():
        legacy_vendor, _ = Vendor.objects.using(database).get_or_create(
            shop_name="Legacy Inventory",
            defaults={
                "full_name": "Legacy Inventory",
                "email": "legacy-inventory@example.invalid",
                "shop_address": "Legacy product owner",
            },
        )
        vendor_ids = [legacy_vendor.pk]

    for index, product in enumerate(
        Product.objects.using(database)
        .filter(vendor__isnull=True)
        .order_by("pk")
        .only("pk")
        .iterator()
    ):
        product.vendor_id = vendor_ids[index % len(vendor_ids)]
        product.save(using=database, update_fields=["vendor"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0002_alter_vendor_user"),
        ("store", "0003_product_sku_product_stock_quantity_product_vendor"),
    ]

    operations = [
        migrations.RunPython(
            assign_legacy_products_to_vendors,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="product",
            name="vendor",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="products",
                to="inventory.vendor",
            ),
        ),
    ]
