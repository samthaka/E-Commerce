import json
from decimal import Decimal
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from unittest.mock import patch

from .admin import ProductAdmin
from .forms import SearchForm
from .models import Customer, Order, OrderItem, Product, ReturnShipment, ShippingModel
from .shippo import order_parcels


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
        self.assertEqual(self.order.status, Order.STATUS_PENDING)


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
            shipping_weight=Decimal("1.00"),
            shipping_length=Decimal("12.00"),
            shipping_width=Decimal("10.00"),
            shipping_height=Decimal("2.00"),
        )
        self.watch = Product.objects.create(
            name="Classic Watch",
            price=Decimal("40.00"),
            category="Watches",
            shipping_weight=Decimal("0.50"),
            shipping_length=Decimal("8.00"),
            shipping_width=Decimal("6.00"),
            shipping_height=Decimal("3.00"),
        )

    def add_shipping_quote(self, order, shipping_data=None, amount="2.50"):
        shipping_data = shipping_data or {
            "address": "1 Main Street",
            "city": "Springfield",
            "state": "IL",
            "zipcode": "62701",
        }
        return ShippingModel.objects.create(
            customer=order.customer,
            order=order,
            recipient_name="Shopper",
            recipient_email="shopper@example.com",
            **shipping_data,
            rates=[{
                "object_id": "rate_test",
                "amount": amount,
                "currency": "USD",
                "provider": "USPS",
                "servicelevel": "Ground Advantage",
            }],
            parcel_manifest=order_parcels(order),
            shippo_shipment_id="shipment_test",
        )

    def test_store_lists_products_and_categories_for_anonymous_user(self):
        response = self.client.get(reverse("store"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "store/store.html")
        self.assertEqual(set(response.context["products"]), {self.bag, self.watch})
        self.assertEqual(response.context["cartItems"], 0)
        self.assertCountEqual(response.context["categories"], ["Accessories", "Watches"])
        self.assertFalse(Customer.objects.exists())
        self.assertContains(response, 'class="product-description" hidden')
        self.assertContains(response, "View")
        self.assertEqual(
            response.content.count(
                f'data-description-target="product-description-{self.bag.pk}"'.encode()
            ),
            2,
        )
        self.assertContains(response, f'href="{reverse("order_history")}"')

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
        self.assertRedirects(checkout_response, reverse("cart"))
        self.assertEqual(cart_response.context["order"]["get_cart_items"], 0)
        self.assertContains(cart_response, 'id="checkout-button"')
        self.assertContains(cart_response, 'disabled aria-disabled="true"')
        self.assertEqual(list(cart_response.context["items"]), [])

    def test_authenticated_empty_cart_cannot_open_checkout(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("checkout"))

        self.assertRedirects(response, reverse("cart"))

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

    def test_checkout_prefills_latest_used_address_and_lists_older_addresses(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        older_order = Order.objects.create(customer=customer, complete=True)
        ShippingModel.objects.create(
            customer=customer,
            order=older_order,
            recipient_name="Shopper",
            address="1 Old Street",
            city="Springfield",
            state="IL",
            zipcode="62701",
        )
        latest_order = Order.objects.create(customer=customer, complete=True)
        latest_shipping = ShippingModel.objects.create(
            customer=customer,
            order=latest_order,
            recipient_name="Shopper",
            address="2 New Street",
            city="Chicago",
            state="IL",
            zipcode="60601",
        )
        current_order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=current_order, product=self.bag, quantity=1)

        response = self.client.get(reverse("checkout"))

        self.assertEqual(response.context["latest_shipping"], latest_shipping)
        self.assertEqual(response.context["saved_addresses"], list(
            ShippingModel.objects.filter(order=older_order)
        ))
        self.assertContains(response, 'value="2 New Street"')
        self.assertContains(response, 'value="Chicago"')
        self.assertContains(response, 'data-address="1 Old Street"')
        self.assertNotContains(response, 'data-address="2 New Street"')

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

    def test_order_history_groups_completed_orders_by_status(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        expected = {}
        for status in (Order.STATUS_DELIVERED, Order.STATUS_PENDING, Order.STATUS_CANCELLED):
            order = Order.objects.create(customer=customer, complete=True, status=status)
            OrderItem.objects.create(order=order, product=self.bag, quantity=1)
            expected[status] = order

        response = self.client.get(reverse("order_history"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "store/order_history.html")
        self.assertContains(response, "Delivered")
        self.assertContains(response, "Pending (in delivery)")
        self.assertContains(response, "Cancelled")
        self.assertContains(response, "Canvas Bag")
        self.assertCountEqual(
            [
                order
                for section in response.context["sections"]
                for order in section["orders"]
            ],
            expected.values(),
        )

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
        self.add_shipping_quote(order)
        payload = {
            "form": {"total": "27.50"},
            "shipping": {
                "rate_id": "rate_test",
                "name": "Shopper",
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
                "email": "shopper@example.com",
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
        self.assertEqual(shipping.shipping_cost, Decimal("2.50"))
        self.assertEqual(shipping.selected_rate_id, "rate_test")
        self.assertEqual(order.get_order_total, Decimal("27.50"))

    def test_order_total_comparison_preserves_decimal_cents(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        product = Product.objects.create(name="Sticker", price=Decimal("0.10"), digital=True)
        OrderItem.objects.create(order=order, product=product, quantity=1)
        shipping = self.add_shipping_quote(order, amount="0.00")
        shipping.rates[0]["object_id"] = "no_shipping_required"
        shipping.parcel_manifest = []
        shipping.save()
        payload = {
            "form": {"total": "0.10"},
            "shipping": {
                "rate_id": "no_shipping_required",
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

    def test_empty_cart_cannot_be_processed_as_an_order(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
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

        order.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(order.complete)
        self.assertFalse(ShippingModel.objects.exists())

    def test_order_with_incorrect_total_is_not_completed_or_shipped(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=2)
        payload = {
            "form": {"total": "1.00"},
            "shipping": {
                "rate_id": "rate_test",
                "name": "Shopper",
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
                "email": "shopper@example.com",
            },
        }
        self.add_shipping_quote(order)

        response = self.client.post(
            reverse("processOrder"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        order.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(order.complete)
        self.assertFalse(order.transaction_id)
        self.assertTrue(ShippingModel.objects.filter(order=order).exists())

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.create_shipment")
    def test_shipping_quote_is_limited_to_valid_us_addresses(self, create_shipment):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        create_shipment.return_value = {
            "object_id": "shipment_test",
            "rates": [{
                "object_id": "rate_test",
                "amount": "4.25",
                "currency": "USD",
                "provider": "USPS",
                "servicelevel": {"name": "Ground Advantage"},
                "estimated_days": 4,
            }],
        }
        address = {
            "name": "Shopper",
            "email": "shopper@example.com",
            "address": "1 Main Street",
            "city": "Springfield",
            "state": "IL",
            "zipcode": "62701",
            "country": "US",
        }

        response = self.client.post(
            reverse("shipping_rates"),
            data=json.dumps(address),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rates"][0]["amount"], "4.25")
        create_shipment.assert_called_once()
        shipping = ShippingModel.objects.get(order=order)
        self.assertEqual(shipping.parcel_manifest, order_parcels(order))

        address["zipcode"] = "123"
        invalid_response = self.client.post(
            reverse("shipping_rates"),
            data=json.dumps(address),
            content_type="application/json",
        )
        self.assertEqual(invalid_response.status_code, 400)
        self.assertEqual(create_shipment.call_count, 1)

    def test_order_cannot_use_stale_shipping_quote_after_cart_changes(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        self.add_shipping_quote(order)
        OrderItem.objects.filter(order=order, product=self.bag).update(quantity=2)

        response = self.client.post(
            reverse("processOrder"),
            data=json.dumps({
                "form": {"total": "15.00"},
                "shipping": {
                    "rate_id": "rate_test",
                    "name": "Shopper",
                    "address": "1 Main Street",
                    "city": "Springfield",
                    "state": "IL",
                    "zipcode": "62701",
                    "email": "shopper@example.com",
                },
            }),
            content_type="application/json",
        )

        order.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(order.complete)

    def test_order_cannot_use_quote_after_shipping_address_changes(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        self.add_shipping_quote(order)
        changed_address = {
            "rate_id": "rate_test",
            "name": "Shopper",
            "address": "2 Other Street",
            "city": "Springfield",
            "state": "IL",
            "zipcode": "62701",
            "email": "shopper@example.com",
        }

        response = self.client.post(
            reverse("processOrder"),
            data=json.dumps({
                "form": {"total": "15.00"},
                "shipping": changed_address,
            }),
            content_type="application/json",
        )

        order.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(order.complete)

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.create_transaction")
    @patch("store.views.ShippoClient.create_shipment")
    def test_customer_can_get_return_rates_and_create_return_label(
        self, create_shipment, create_transaction
    ):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer, complete=True)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        shipping = self.add_shipping_quote(order)
        create_shipment.return_value = {
            "object_id": "return_shipment_test",
            "rates": [{
                "object_id": "return_rate_test",
                "amount": "3.75",
                "currency": "USD",
                "provider": "USPS",
                "servicelevel": "Ground Advantage",
            }],
        }
        create_transaction.return_value = {
            "status": "SUCCESS",
            "tracking_number": "TEST123",
            "tracking_url_provider": "https://tracking.example/test123",
            "label_url": "https://labels.example/test123.pdf",
            "provider": "USPS",
        }

        rates_response = self.client.post(
            reverse("return_rates", args=[order.pk]),
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(rates_response.status_code, 200)
        return_shipment = ReturnShipment.objects.get(shipping=shipping)
        label_response = self.client.post(
            reverse("purchase_return_label", args=[order.pk, return_shipment.pk]),
            data=json.dumps({"rate_id": "return_rate_test"}),
            content_type="application/json",
        )

        self.assertEqual(label_response.status_code, 200)
        self.assertEqual(label_response.json()["tracking_number"], "TEST123")
        return_shipment.refresh_from_db()
        self.assertEqual(return_shipment.shipping_cost, Decimal("3.75"))
        self.assertEqual(return_shipment.selected_rate_id, "return_rate_test")
        create_shipment.assert_called_once()
        create_transaction.assert_called_once_with("return_rate_test")

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.track")
    def test_customer_can_refresh_tracking_and_delivery_updates_order(self, track):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer, complete=True)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        shipping = self.add_shipping_quote(order)
        shipping.tracking_number = "TEST123"
        shipping.carrier = "USPS"
        shipping.save()
        track.return_value = {
            "tracking_status": {
                "status": "DELIVERED",
                "status_details": "Package delivered",
            }
        }

        response = self.client.post(
            reverse("refresh_tracking", args=[order.pk]),
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "DELIVERED")
        shipping.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(shipping.tracking_status, "DELIVERED")
        self.assertEqual(order.status, Order.STATUS_DELIVERED)
        track.assert_called_once_with("USPS", "TEST123")


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
