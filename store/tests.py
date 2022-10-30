import json
from decimal import Decimal
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .admin import ProductAdmin
from .forms import SearchForm
from .models import Customer, Order, OrderItem, Product, ShippingModel


class StoreModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="customer", password="secure-pass")
        self.customer = Customer.objects.create(
            user=self.user,
            name="Test Customer",
            email="customer@example.com",
        )
        self.product = Product.objects.create(
            name="Canvas Bag",
            price=Decimal("12.50"),
            category="Accessories",
        )
        self.order = Order.objects.create(customer=self.customer)

    def test_customer_and_product_string_representations(self):
        self.assertEqual(str(self.customer), "Test Customer")
        self.assertEqual(str(self.product), "Canvas Bag")

    def test_customer_without_name_has_safe_string_representation(self):
        customer = Customer.objects.create(email="anonymous@example.com")
        self.assertEqual(str(customer), f"Customer {customer.pk}")

    def test_product_without_image_has_empty_image_url(self):
        self.assertEqual(self.product.imageURL, "")

    def test_order_totals_sum_item_quantities_and_prices(self):
        OrderItem.objects.create(order=self.order, product=self.product, quantity=2)
        second_product = Product.objects.create(name="Cap", price=Decimal("7.25"))
        OrderItem.objects.create(order=self.order, product=second_product, quantity=3)

        self.assertEqual(self.order.get_cart_items, 5)
        self.assertEqual(self.order.get_cart_total, Decimal("46.75"))

    def test_order_item_total_and_shipping_string(self):
        item = OrderItem.objects.create(order=self.order, product=self.product, quantity=2)
        shipping = ShippingModel.objects.create(
            customer=self.customer,
            order=self.order,
            address="1 Main Street",
        )

        self.assertEqual(item.get_total, Decimal("25.00"))
        self.assertEqual(str(shipping), "1 Main Street")
        self.assertEqual(str(self.order), "None")


class StoreViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="shopper",
            email="shopper@example.com",
            password="secure-pass",
        )
        self.bag = Product.objects.create(
            name="Canvas Bag",
            price=Decimal("12.50"),
            category="Accessories",
        )
        self.watch = Product.objects.create(
            name="Classic Watch",
            price=Decimal("40.00"),
            category="Watches",
        )

    def test_store_lists_products_and_categories_for_anonymous_user(self):
        response = self.client.get(reverse("store"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "store/store.html")
        self.assertEqual(set(response.context["products"]), {self.bag, self.watch})
        self.assertEqual(response.context["cartItems"], 0)
        self.assertCountEqual(response.context["categories"], ["Accessories", "Watches"])
        self.assertFalse(Customer.objects.exists())

    def test_store_filters_products_by_category(self):
        response = self.client.get(reverse("store"), {"category": "Watches"})

        self.assertEqual(list(response.context["products"]), [self.watch])
        self.assertEqual(response.context["selected_category"], "Watches")

    def test_authenticated_store_creates_customer_and_open_order(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("store"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Customer.objects.get(user=self.user).order_set.count(), 1)
        self.assertEqual(response.context["cartItems"], 0)

    def test_guest_cart_and_checkout_render_empty_cart(self):
        cart_response = self.client.get(reverse("cart"))
        checkout_response = self.client.get(reverse("checkout"))

        self.assertEqual(cart_response.status_code, 200)
        self.assertEqual(checkout_response.status_code, 200)
        self.assertEqual(cart_response.context["order"]["get_cart_items"], 0)
        self.assertEqual(checkout_response.context["email"], None)
        self.assertEqual(list(cart_response.context["items"]), [])

    def test_authenticated_cart_and_checkout_show_open_order_items(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=2)

        cart_response = self.client.get(reverse("cart"))
        checkout_response = self.client.get(reverse("checkout"))

        self.assertEqual(cart_response.context["order"], order)
        self.assertEqual(cart_response.context["order"].get_cart_items, 2)
        self.assertEqual(list(checkout_response.context["items"]), list(order.orderitem_set.all()))
        self.assertEqual(checkout_response.context["email"], self.user.email)

    def test_search_filters_by_product_name_and_category(self):
        name_response = self.client.get(reverse("search"), {"query": "canvas"})
        category_response = self.client.get(reverse("search"), {"query": "watches"})

        self.assertEqual(name_response.status_code, 200)
        self.assertTemplateUsed(name_response, "store/store.html")
        self.assertEqual(list(name_response.context["products"]), [self.bag])
        self.assertEqual(list(category_response.context["products"]), [self.watch])
        self.assertEqual(name_response.context["query"], "canvas")

    def test_search_without_query_lists_all_products(self):
        response = self.client.get(reverse("search"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context["products"]), {self.bag, self.watch})

    def test_search_form_validates_query_length(self):
        self.assertTrue(SearchForm(data={"query": "bag"}).is_valid())
        self.assertFalse(SearchForm(data={"query": "x" * 101}).is_valid())

    def test_authenticated_update_item_adds_and_removes_cart_items(self):
        self.client.force_login(self.user)
        url = reverse("updateitem")

        add_response = self.client.post(
            url,
            data=json.dumps({"productId": self.bag.pk, "action": "add"}),
            content_type="application/json",
        )
        item = OrderItem.objects.get(product=self.bag)
        self.assertEqual(add_response.status_code, 200)
        self.assertEqual(item.quantity, 1)

        self.client.post(
            url,
            data=json.dumps({"productId": self.bag.pk, "action": "add"}),
            content_type="application/json",
        )
        item.refresh_from_db()
        self.assertEqual(item.quantity, 2)

        self.client.post(
            url,
            data=json.dumps({"productId": self.bag.pk, "action": "remove"}),
            content_type="application/json",
        )
        item.refresh_from_db()
        self.assertEqual(item.quantity, 1)

        self.client.post(
            url,
            data=json.dumps({"productId": self.bag.pk, "action": "remove"}),
            content_type="application/json",
        )
        self.assertFalse(OrderItem.objects.filter(product=self.bag).exists())

    def test_anonymous_user_cannot_update_cart(self):
        response = self.client.post(
            reverse("updateitem"),
            data=json.dumps({"productId": self.bag.pk, "action": "add"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertFalse(OrderItem.objects.exists())

    def test_update_item_rejects_unknown_action(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("updateitem"),
            data=json.dumps({"productId": self.bag.pk, "action": "delete"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(OrderItem.objects.exists())

    def test_authenticated_order_creates_shipping_and_completes_order(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=2)
        payload = {
            "form": {"total": "25.00"},
            "shipping": {
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
            },
        }

        response = self.client.post(
            reverse("processOrder"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        order.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(order.complete)
        self.assertTrue(order.transaction_id)
        shipping = ShippingModel.objects.get(order=order)
        self.assertEqual(shipping.address, "1 Main Street")
        self.assertEqual(shipping.customer, customer)

    def test_order_total_comparison_preserves_decimal_cents(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        product = Product.objects.create(name="Sticker", price=Decimal("0.10"))
        OrderItem.objects.create(order=order, product=product, quantity=1)
        payload = {
            "form": {"total": "0.10"},
            "shipping": {
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
            },
        }

        response = self.client.post(
            reverse("processOrder"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        order.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(order.complete)

    def test_anonymous_order_is_rejected_without_creating_records(self):
        payload = {
            "form": {"total": "0"},
            "shipping": {
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
            },
        }

        response = self.client.post(
            reverse("processOrder"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(Order.objects.exists())
        self.assertFalse(ShippingModel.objects.exists())

    def test_order_with_incorrect_total_is_not_completed_or_shipped(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=2)
        payload = {
            "form": {"total": "1.00"},
            "shipping": {
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
            },
        }

        response = self.client.post(
            reverse("processOrder"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        order.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(order.complete)
        self.assertFalse(order.transaction_id)
        self.assertFalse(ShippingModel.objects.exists())


class AuthenticationAndAccountTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="account-user",
            email="account@example.com",
            password="secure-pass",
        )

    def test_login_and_signup_pages_render(self):
        login_response = self.client.get(reverse("account_login"))
        signup_response = self.client.get(reverse("account_signup"))

        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(signup_response.status_code, 200)
        self.assertContains(login_response, "Sign in")
        self.assertContains(signup_response, "Create your account")

    def test_authenticated_customer_can_view_profile(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("account_profile"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "account/profile.html")
        self.assertContains(response, "account-user")
        self.assertContains(response, "account@example.com")
        self.assertEqual(response.context["cartItems"], 0)

    def test_profile_uses_open_order_cart_count(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        product = Product.objects.create(name="Book", price=Decimal("8.00"))
        OrderItem.objects.create(order=order, product=product, quantity=3)

        response = self.client.get(reverse("account_profile"))

        self.assertEqual(response.context["cartItems"], 3)

    def test_anonymous_user_is_redirected_from_profile_to_login(self):
        response = self.client.get(reverse("account_profile"))

        self.assertRedirects(
            response,
            f"{reverse('account_login')}?next={reverse('account_profile')}",
        )

    def test_authenticated_user_can_sign_out(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("account_logout"))

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)


class AdminTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(
            name="Admin Test Product",
            price=Decimal("15.00"),
            category="Test",
        )

    def test_store_models_are_registered_with_admin(self):
        for model in (Customer, Product, Order, OrderItem, ShippingModel):
            with self.subTest(model=model):
                self.assertIn(model, admin.site._registry)

        product_admin = admin.site._registry[Product]
        self.assertIsInstance(product_admin, ProductAdmin)
        self.assertIn("name", product_admin.search_fields)
        self.assertEqual(
            product_admin.image_preview(self.product),
            "",
        )
        product_with_image = SimpleNamespace(
            image=SimpleNamespace(url="/images/product.jpg"),
        )
        self.assertEqual(
            product_admin.image_preview(product_with_image),
            '<img src="/images/product.jpg" style="width: 100px;"/>',
        )

    def test_admin_site_labels_are_configured(self):
        self.assertEqual(admin.site.site_header, "E-commerce")
        self.assertEqual(admin.site.site_title, "E-commerce")
        self.assertEqual(admin.site.index_title, "Admin")

    def test_admin_requires_staff_access(self):
        response = self.client.get(reverse("admin:store_product_changelist"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response.url)

    def test_superuser_can_manage_products(self):
        staff_user = User.objects.create_superuser(
            username="store-admin",
            email="admin@example.com",
            password="secure-pass",
        )
        self.client.force_login(staff_user)

        response = self.client.get(reverse("admin:store_product_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Admin Test Product")
