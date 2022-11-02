from django.db import models
from django.contrib.auth.models import User
from decimal import Decimal

# Create your models here.

class Customer(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE,null=True, blank=True)
    name = models.CharField(max_length=200, null=True)
    email = models.CharField(max_length=200, null=True)

    def __str__(self):
        return self.name or (f"Customer {self.pk}" if self.pk else "Customer")


class Product(models.Model):
    name = models.CharField(max_length=200, null=True)
    price = models.DecimalField(max_digits=7, decimal_places=2)
    digital = models.BooleanField(default=False, null=True, blank=False)
    shipping_weight = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Packed item weight in pounds, required for physical products.",
    )
    shipping_length = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Packed length in inches, required for physical products.",
    )
    shipping_width = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Packed width in inches, required for physical products.",
    )
    shipping_height = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Packed height in inches, required for physical products.",
    )
    image = models.ImageField(null=True,blank=True)
    description = models.TextField(null=True)
    category = models.CharField(max_length=200, null=True)

    def __str__(self):
        return self.name

    @property
    def imageURL(self):
        try:
            url = self.image.url
        except:
            url = ''
        return url


class Order(models.Model):
    STATUS_PENDING = "pending"
    STATUS_DELIVERED = "delivered"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending (in delivery)"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, blank=True, null=True)
    date_ordered = models.DateTimeField(auto_now_add=True)
    complete = models.BooleanField(default=False, null=True, blank=False)
    transaction_id = models.CharField(max_length=200, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)

    def __str__(self):
        return str(self.transaction_id)


    @property
    def get_cart_total(self):
        orderitems = self.orderitem_set.all()
        total = sum([item.get_total for item in orderitems])
        return total

    @property
    def get_cart_items(self):
        orderitems = self.orderitem_set.all()
        total = sum([item.quantity for item in orderitems])
        return total

    @property
    def get_shipping_total(self):
        return sum(
            (shipment.shipping_cost for shipment in self.shippingmodel_set.all()),
            Decimal("0.00"),
        )

    @property
    def get_order_total(self):
        return self.get_cart_total + self.get_shipping_total


class OrderItem(models.Model):
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True)
    quantity = models.IntegerField(default=0, null=True, blank=True)
    date_added = models.DateTimeField(auto_now_add=True)

    @property
    def get_total(self):
        total = self.product.price * self.quantity
        return total
    

class ShippingModel(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, blank=True, null=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, blank=True, null=True)
    address = models.CharField(max_length=200, null=True)
    recipient_name = models.CharField(max_length=200, blank=True)
    recipient_email = models.EmailField(blank=True)
    city = models.CharField(max_length=200, null=True)
    state = models.CharField(max_length=200, null=True)
    zipcode = models.CharField(max_length=200, null=True)
    country = models.CharField(max_length=2, default="US")
    shippo_shipment_id = models.CharField(max_length=100, blank=True)
    rates = models.JSONField(default=list, blank=True)
    parcel_manifest = models.JSONField(default=list, blank=True)
    selected_rate_id = models.CharField(max_length=100, blank=True)
    shipping_cost = models.DecimalField(max_digits=9, decimal_places=2, default=Decimal("0.00"))
    carrier = models.CharField(max_length=100, blank=True)
    service_level = models.CharField(max_length=100, blank=True)
    tracking_number = models.CharField(max_length=200, blank=True)
    tracking_url = models.URLField(blank=True)
    tracking_status = models.CharField(max_length=100, blank=True)
    tracking_status_details = models.CharField(max_length=255, blank=True)
    label_url = models.URLField(blank=True)
    date_added = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.address


class ReturnShipment(models.Model):
    shipping = models.ForeignKey(ShippingModel, on_delete=models.CASCADE, related_name="returns")
    shippo_shipment_id = models.CharField(max_length=100, blank=True)
    rates = models.JSONField(default=list, blank=True)
    selected_rate_id = models.CharField(max_length=100, blank=True)
    shipping_cost = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    carrier = models.CharField(max_length=100, blank=True)
    service_level = models.CharField(max_length=100, blank=True)
    tracking_number = models.CharField(max_length=200, blank=True)
    tracking_url = models.URLField(blank=True)
    tracking_status = models.CharField(max_length=100, blank=True)
    tracking_status_details = models.CharField(max_length=255, blank=True)
    label_url = models.URLField(blank=True)
    date_added = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Return for order {self.shipping.order_id}"