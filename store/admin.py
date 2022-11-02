from django.contrib import admin
from django.contrib import messages
from .models import *
from .shippo import ShippoClient, ShippoError, transaction_fields

from django.utils.html import mark_safe

admin.site.site_header = 'E-commerce'
admin.site.site_title = 'E-commerce'
admin.site.index_title = 'Admin'

class ProductAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'price',
        'digital',
        'shipping_weight',
        'shipping_length',
        'shipping_width',
        'shipping_height',
        'image_preview',
    )
    search_fields = ('name',)
    list_filter = ('price', 'digital')
    readonly_fields = ('image_preview',)

    def image_preview(self, obj):
        if obj.image:
            return mark_safe('<img src="{}" style="width: 100px;"/>'.format(obj.image.url))
        return ""
    image_preview.short_description = 'Image Preview'


admin.site.register(Customer)
admin.site.register(Product,ProductAdmin)
@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "date_ordered", "complete", "status")
    list_filter = ("complete", "status")
    actions = ("create_shippo_labels",)

    @admin.action(description="Create Shippo labels for selected orders")
    def create_shippo_labels(self, request, queryset):
        created = 0
        for order in queryset.filter(complete=True):
            shipping = order.shippingmodel_set.order_by("-date_added").first()
            if shipping is None or not shipping.selected_rate_id or shipping.label_url:
                continue
            try:
                transaction = ShippoClient().create_transaction(shipping.selected_rate_id)
            except ShippoError as exc:
                self.message_user(
                    request,
                    f"Order #{order.pk}: {exc}",
                    level=messages.ERROR,
                )
                continue
            for field, value in transaction_fields(transaction).items():
                setattr(shipping, field, value)
            shipping.save()
            created += 1
        if created:
            self.message_user(
                request,
                f"Created {created} Shippo label(s).",
                level=messages.SUCCESS,
            )
admin.site.register(OrderItem)
admin.site.register(ShippingModel)
admin.site.register(ReturnShipment)
