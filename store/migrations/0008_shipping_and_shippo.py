from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0007_order_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="shipping_weight",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Packed item weight in pounds, required for physical products.",
                max_digits=7,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="shipping_length",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Packed length in inches, required for physical products.",
                max_digits=7,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="shipping_width",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Packed width in inches, required for physical products.",
                max_digits=7,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="shipping_height",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Packed height in inches, required for physical products.",
                max_digits=7,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="recipient_name",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="recipient_email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="country",
            field=models.CharField(default="US", max_length=2),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="shippo_shipment_id",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="rates",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="parcel_manifest",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="selected_rate_id",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="shipping_cost",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=9
            ),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="carrier",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="service_level",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="tracking_number",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="tracking_url",
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="tracking_status",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="tracking_status_details",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="shippingmodel",
            name="label_url",
            field=models.URLField(blank=True),
        ),
        migrations.CreateModel(
            name="ReturnShipment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("shippo_shipment_id", models.CharField(blank=True, max_length=100)),
                ("rates", models.JSONField(blank=True, default=list)),
                ("selected_rate_id", models.CharField(blank=True, max_length=100)),
                (
                    "shipping_cost",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=9, null=True
                    ),
                ),
                ("carrier", models.CharField(blank=True, max_length=100)),
                ("service_level", models.CharField(blank=True, max_length=100)),
                ("tracking_number", models.CharField(blank=True, max_length=200)),
                ("tracking_url", models.URLField(blank=True)),
                ("tracking_status", models.CharField(blank=True, max_length=100)),
                (
                    "tracking_status_details",
                    models.CharField(blank=True, max_length=255),
                ),
                ("label_url", models.URLField(blank=True)),
                ("date_added", models.DateTimeField(auto_now_add=True)),
                (
                    "shipping",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="returns",
                        to="store.shippingmodel",
                    ),
                ),
            ],
        ),
    ]
