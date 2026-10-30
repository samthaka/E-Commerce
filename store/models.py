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


class Category(models.Model):
    """Three-level tree: master category > sub category > article type.
    Products attach to the leaf (article type)."""

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=160, unique=True)  # full path, e.g. "apparel-topwear-shirts"
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "name"], name="unique_category_name_per_parent"
            ),
        ]

    def __str__(self):
        return self.full_path

    @property
    def full_path(self):
        parts, node = [], self
        while node:
            parts.append(node.name)
            node = node.parent
        return " > ".join(reversed(parts))

    def descendant_ids(self):
        """This category's id plus all descendant ids (use for 'all products under X')."""
        ids, frontier = [self.pk], [self.pk]
        while frontier:
            frontier = list(
                Category.objects.filter(parent_id__in=frontier).values_list("pk", flat=True)
            )
            ids.extend(frontier)
        return ids


class Product(models.Model):
    GENDER_CHOICES = [
        ("Men", "Men"),
        ("Women", "Women"),
        ("Unisex", "Unisex"),
        ("Boys", "Boys"),
        ("Girls", "Girls"),
    ]

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
    image = models.ImageField(null=True, blank=True)    
    description = models.TextField(null=True)
    category = models.ForeignKey(
        Category,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products",
    )

    # Attributes from the Myntra dataset
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True)
    base_colour = models.CharField(max_length=50, blank=True)
    season = models.CharField(max_length=20, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    usage = models.CharField(max_length=30, blank=True)

    # Import bookkeeping
    source_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        unique=True,
        help_text="ID from the Kaggle/Myntra dataset; lets the import be re-run safely.",
    )
    is_synthetic = models.BooleanField(
        default=False,
        help_text="Price, description and shipping data were generated, not real.",
    )

    class Meta:
        indexes = [
            models.Index(fields=["gender"]),
            models.Index(fields=["base_colour"]),
        ]

    def __str__(self):
        return self.name or f"Product {self.pk}"

    @property
    def imageURL(self):
        try:
            url = self.image.url
        except ValueError:
            url = ""
        return url

    @property
    def average_rating(self):
        reviews = self.reviews.all()
        if not reviews.exists():
            return 0
        return round(sum(review.rating for review in reviews) / reviews.count(), 1)

    @property
    def review_count(self):
        return self.reviews.count()

    @property
    def latest_review(self):
        return self.reviews.order_by("-created_at").first()


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

    @property
    def review(self):
        if not self.order or not self.product or not self.order.customer:
            return None
        return self.product.reviews.filter(order=self.order, customer=self.order.customer).order_by('-created_at').first()


class ProductReview(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='reviews')
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='product_reviews')
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='product_reviews')
    rating = models.PositiveSmallIntegerField(default=5)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['product', 'customer', 'order'],
                name='unique_product_review_per_customer_order',
            )
        ]

    def __str__(self):
        return f"{self.customer} rated {self.product} {self.rating}/5"


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