from django.contrib import admin
from .models import *

from django.utils.html import mark_safe

class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'price', 'description', 'image_preview')
    search_fields = ('name',)
    list_filter = ('price', 'description')
    readonly_fields = ('image_preview',)

    def image_preview(self, obj):
        if obj.image:
            return mark_safe('<img src="{}" style="width: 100px;"/>'.format(obj.image.url))
        return ""
    image_preview.short_description = 'Image Preview'


admin.site.register(Customer)
admin.site.register(Product,ProductAdmin)
admin.site.register(Order)
admin.site.register(OrderItem)
admin.site.register(ShippingModel)

