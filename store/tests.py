import json
from decimal import Decimal
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from unittest.mock import patch

import stripe

from .admin import OrderAdmin, ProductAdmin
from .forms import SearchForm
from .models import Customer, Order, OrderItem, Product, ProductReview, ReturnShipment, ShippingModel
from .shippo import (
    InvalidParcelError,
    ShippoClient,
    ShippoError,
    find_rate,
    order_parcels,
    origin_address,
    recipient_address,
    serialize_rates,
    transaction_fields,
)


class ShippoClientTests(TestCase):
    @override_settings(SHIPPO_API_TOKEN="", SHIPPO_TEST_MODE=True)
    def test_client_requires_an_api_token(self):
        with self.assertRaisesMessage(ShippoError, "Shippo API token is not configured."):
            ShippoClient()

    @override_settings(SHIPPO_API_TOKEN="live_token", SHIPPO_TEST_MODE=True)
    def test_test_mode_requires_a_test_api_token(self):
        with self.assertRaisesMessage(
            ShippoError,
            "Shippo test mode requires a test API token.",
        ):
            ShippoClient()

    @override_settings(
        SHIPPO_API_TOKEN="shippo_test_token",
        SHIPPO_TEST_MODE=True,
        SHIPPO_API_URL="https://shippo.example/",
    )
    @patch("store.shippo.requests.request")
    def test_request_returns_json_and_sends_authentication_and_payload(self, request):
        response = SimpleNamespace(ok=True, json=lambda: {"object_id": "shipment"})
        request.return_value = response

        result = ShippoClient().request("POST", "/shipments/", {"async": False})

        self.assertEqual(result, {"object_id": "shipment"})
        request.assert_called_once_with(
            "POST",
            "https://shippo.example/shipments/",
            headers={
                "Authorization": "ShippoToken shippo_test_token",
                "Content-Type": "application/json",
            },
            json={"async": False},
            timeout=20,
        )

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.shippo.requests.request")
    def test_request_translates_transport_and_response_errors(self, request):
        request.side_effect = ShippoClientTests.request_exception()
        with self.assertRaisesMessage(
            ShippoError,
            "Shippo could not be reached. Please try again.",
        ):
            ShippoClient().request("GET", "/tracks/")

        cases = (
            (SimpleNamespace(ok=True, json=self.raise_json_error), "Shippo returned an unreadable response."),
            (SimpleNamespace(ok=True, json=lambda: []), "Shippo returned an unexpected response."),
            (SimpleNamespace(ok=False, json=lambda: {"detail": "Invalid address"}), "Invalid address"),
            (SimpleNamespace(ok=False, json=lambda: {"message": "Rate limit"}), "Rate limit"),
            (SimpleNamespace(ok=False, json=lambda: {}), "Shippo request failed."),
        )
        for response, message in cases:
            with self.subTest(message=message):
                request.side_effect = None
                request.return_value = response
                with self.assertRaisesMessage(ShippoError, message):
                    ShippoClient().request("GET", "/tracks/")

    @staticmethod
    def raise_json_error():
        raise ValueError("invalid json")

    @staticmethod
    def request_exception():
        import requests

        return requests.Timeout("timeout")

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.shippo.requests.request")
    def test_shipment_and_transaction_send_expected_payloads(self, request):
        request.return_value = SimpleNamespace(ok=True, json=lambda: {"status": "SUCCESS"})
        client = ShippoClient()

        shipment = client.create_shipment({"name": "Sender"}, {"name": "Buyer"}, [{"weight": "1"}])
        transaction = client.create_transaction("rate_123")

        self.assertEqual(shipment, {"status": "SUCCESS"})
        self.assertEqual(transaction, {"status": "SUCCESS"})
        self.assertEqual(request.call_args_list[0].args, ("POST", "https://api.goshippo.com/shipments/"))
        self.assertEqual(request.call_args_list[0].kwargs["json"], {
            "address_from": {"name": "Sender"},
            "address_to": {"name": "Buyer"},
            "parcels": [{"weight": "1"}],
            "async": False,
        })
        self.assertEqual(request.call_args_list[1].args, ("POST", "https://api.goshippo.com/transactions/"))
        self.assertEqual(request.call_args_list[1].kwargs["json"], {
            "rate": "rate_123",
            "label_file_type": "PDF",
            "async": False,
        })

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.shippo.requests.request")
    def test_transaction_rejects_failed_label_purchase(self, request):
        request.return_value = SimpleNamespace(
            ok=True,
            json=lambda: {"status": "ERROR", "messages": ["Invalid rate"]},
        )

        with self.assertRaisesMessage(ShippoError, "['Invalid rate']"):
            ShippoClient().create_transaction("bad_rate")

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.shippo.requests.request")
    def test_tracking_requires_both_carrier_and_tracking_number(self, request):
        client = ShippoClient()
        for carrier, tracking_number in (("", "123"), ("USPS", "")):
            with self.subTest(carrier=carrier, tracking_number=tracking_number):
                with self.assertRaisesMessage(
                    ShippoError,
                    "A carrier and tracking number are required.",
                ):
                    client.track(carrier, tracking_number)
        request.assert_not_called()

        request.return_value = SimpleNamespace(
            ok=True,
            json=lambda: {"tracking_status": {"status": "TRANSIT"}},
        )
        self.assertEqual(
            client.track("USPS", "TRACK123"),
            {"tracking_status": {"status": "TRANSIT"}},
        )
        self.assertEqual(
            request.call_args.args[:2],
            ("POST", "https://api.goshippo.com/tracks/usps/TRACK123/"),
        )

    def test_rate_and_transaction_serializers(self):
        rates = serialize_rates([
            {"object_id": "rate_1", "amount": "4.25"},
            {"object_id": "rate_missing_amount"},
            {"amount": "1.00"},
            {
                "object_id": "rate_2",
                "amount": "5.00",
                "servicelevel": "Priority",
                "provider": "USPS",
                "estimated_days": 2,
                "duration_terms": "Ships in two days",
            },
        ])

        self.assertEqual(rates, [
            {
                "object_id": "rate_1",
                "amount": "4.25",
                "currency": "USD",
                "provider": "",
                "servicelevel": "",
                "estimated_days": None,
                "duration_terms": "",
            },
            {
                "object_id": "rate_2",
                "amount": "5.00",
                "currency": "USD",
                "provider": "USPS",
                "servicelevel": "Priority",
                "estimated_days": 2,
                "duration_terms": "Ships in two days",
            },
        ])
        self.assertEqual(find_rate(rates, "rate_2"), rates[1])
        self.assertIsNone(find_rate(rates, "missing"))
        self.assertEqual(
            serialize_rates([{
                "object_id": "rate_3",
                "amount": "6.00",
                "servicelevel": {"name": "Ground"},
            }])[0]["servicelevel"],
            "Ground",
        )
        self.assertEqual(
            transaction_fields({
                "tracking_number": "TRACK123",
                "tracking_url_provider": "https://tracking.example/123",
                "label_url": "https://labels.example/123.pdf",
                "carrier": "USPS",
            }),
            {
                "tracking_number": "TRACK123",
                "tracking_url": "https://tracking.example/123",
                "label_url": "https://labels.example/123.pdf",
                "carrier": "USPS",
                "tracking_status": "pre_transit",
            },
        )
        self.assertEqual(transaction_fields({"provider": "UPS"}), {"carrier": "UPS"})

    @override_settings(
        SHIPPO_FROM_NAME="Warehouse",
        SHIPPO_FROM_EMAIL="warehouse@example.com",
        SHIPPO_FROM_STREET="10 Main St",
        SHIPPO_FROM_CITY="Chicago",
        SHIPPO_FROM_STATE="IL",
        SHIPPO_FROM_ZIP="60601",
    )
    def test_address_serializers(self):
        self.assertEqual(origin_address(), {
            "name": "Warehouse",
            "street1": "10 Main St",
            "city": "Chicago",
            "state": "IL",
            "zip": "60601",
            "country": "US",
            "email": "warehouse@example.com",
        })
        self.assertEqual(
            recipient_address({"name": "Buyer", "address": "1 Oak St"}, "buyer@example.com"),
            {
                "name": "Buyer",
                "street1": "1 Oak St",
                "city": "",
                "state": "",
                "zip": "",
                "country": "US",
                "email": "buyer@example.com",
            },
        )

    def test_order_parcels_skips_digital_items_and_builds_each_physical_parcel(self):
        customer = Customer.objects.create()
        order = Order.objects.create(customer=customer)
        physical = Product.objects.create(
            name="Physical",
            price=Decimal("10.00"),
            shipping_weight=Decimal("1.25"),
            shipping_length=Decimal("10.00"),
            shipping_width=Decimal("8.00"),
            shipping_height=Decimal("2.00"),
        )
        digital = Product.objects.create(name="Digital", price=Decimal("5.00"), digital=True)
        OrderItem.objects.create(order=order, product=physical, quantity=2)
        OrderItem.objects.create(order=order, product=digital, quantity=1)
        OrderItem.objects.create(order=order, product=None, quantity=1)

        self.assertEqual(order_parcels(order), [
            {
                "length": "10.00",
                "width": "8.00",
                "height": "2.00",
                "distance_unit": "in",
                "weight": "1.25",
                "mass_unit": "lb",
            },
        ] * 2)

    def test_order_parcels_rejects_missing_dimensions_and_invalid_quantity(self):
        customer = Customer.objects.create()
        order = Order.objects.create(customer=customer)
        product = Product.objects.create(name="Unpacked", price=Decimal("10.00"))
        item = OrderItem.objects.create(order=order, product=product, quantity=1)

        with self.assertRaisesMessage(InvalidParcelError, "Shipping weight and dimensions are required for Unpacked."):
            order_parcels(order)

        product.shipping_weight = Decimal("1.00")
        product.shipping_length = Decimal("1.00")
        product.shipping_width = Decimal("1.00")
        product.shipping_height = Decimal("1.00")
        product.save()
        item.quantity = 0
        item.save()
        with self.assertRaisesMessage(InvalidParcelError, "A valid quantity is required for Unpacked."):
            order_parcels(order)


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

    def test_product_review_summary_and_latest_review(self):
        review = ProductReview.objects.create(
            product=self.product,
            customer=self.customer,
            order=self.order,
            rating=5,
            message="A great find.",
        )

        self.assertEqual(self.product.average_rating, 5.0)
        self.assertEqual(self.product.review_count, 1)
        self.assertEqual(self.product.latest_review, review)
        self.assertEqual(self.product.latest_review.rating, 5)


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

    def payment_intent(self, order, amount, rate_id="rate_test", status="succeeded"):
        return SimpleNamespace(
            id="pi_test_payment",
            status=status,
            amount=amount,
            amount_received=amount,
            currency="usd",
            metadata=stripe.StripeObject.construct_from({
                "order_id": str(order.pk),
                "user_id": str(self.user.pk),
                "rate_id": rate_id,
            }, None),
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

    @override_settings(
        STRIPE_SECRET_KEY="sk_test_example",
        STRIPE_PUBLISHABLE_KEY="pk_test_example",
    )
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

        payload["payment_intent_id"] = "pi_test_payment"
        with patch("store.views.StripeClient") as stripe_client:
            stripe_client.return_value.v1.payment_intents.retrieve.return_value = (
                self.payment_intent(order, 2750)
            )
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

    @override_settings(
        STRIPE_SECRET_KEY="sk_test_example",
        STRIPE_PUBLISHABLE_KEY="pk_test_example",
    )
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

        payload["payment_intent_id"] = "pi_test_payment"
        with patch("store.views.StripeClient") as stripe_client:
            stripe_client.return_value.v1.payment_intents.retrieve.return_value = (
                self.payment_intent(order, 10, rate_id="no_shipping_required")
            )
            response = self.client.post(
                reverse("processOrder"),
                data=json.dumps(payload),
                content_type="application/json",
            )

        order.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(order.complete)

    @override_settings(
        STRIPE_SECRET_KEY="sk_test_example",
        STRIPE_PUBLISHABLE_KEY="pk_test_example",
    )
    def test_payment_intent_uses_server_calculated_order_total(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=2)
        self.add_shipping_quote(order)
        intent = SimpleNamespace(
            id="pi_test_payment",
            client_secret="pi_test_payment_secret",
        )

        with patch("store.views.StripeClient") as stripe_client:
            stripe_client.return_value.v1.payment_intents.create.return_value = intent
            response = self.client.post(
                reverse("create_payment_intent"),
                data=json.dumps({
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

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["amount"], 2750)
        self.assertEqual(response.json()["client_secret"], "pi_test_payment_secret")
        self.assertEqual(response.json()["publishable_key"], "pk_test_example")
        stripe_client.return_value.v1.payment_intents.create.assert_called_once()
        params = stripe_client.return_value.v1.payment_intents.create.call_args.kwargs["params"]
        self.assertEqual(params["amount"], 2750)
        self.assertEqual(params["currency"], "usd")
        self.assertEqual(params["metadata"]["order_id"], str(order.pk))

    @override_settings(
        STRIPE_SECRET_KEY="sk_test_example",
        STRIPE_PUBLISHABLE_KEY="pk_test_example",
    )
    def test_unpaid_or_wrong_amount_payment_intent_cannot_complete_order(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        self.add_shipping_quote(order)

        with patch("store.views.StripeClient") as stripe_client:
            stripe_client.return_value.v1.payment_intents.retrieve.side_effect = [
                self.payment_intent(order, 0, status="requires_payment_method"),
                self.payment_intent(order, 1250),
            ]
            for _ in range(2):
                response = self.client.post(
                    reverse("processOrder"),
                    data=json.dumps({"payment_intent_id": "pi_test_payment"}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
                order.refresh_from_db()
                self.assertFalse(order.complete)

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

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.create_shipment")
    def test_shipping_quote_logs_shippo_messages_for_multiple_item_cart(self, create_shipment):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=15)
        create_shipment.return_value = {
            "object_id": "shipment_test",
            "rates": [],
            "messages": [{"text": "Carrier does not support this parcel combination."}],
        }

        with self.assertLogs("store.views", level="WARNING") as logged:
            response = self.client.post(
                reverse("shipping_rates"),
                data=json.dumps({
                    "name": "Shopper",
                    "address": "1 Main Street",
                    "city": "Springfield",
                    "state": "IL",
                    "zipcode": "62701",
                }),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["error"],
            "No shipping rates are available right now. Please try again later.",
        )
        self.assertIn("Carrier does not support this parcel combination.", logged.output[0])
        parcels = create_shipment.call_args.args[2]
        self.assertEqual(len(parcels), 15)
        self.assertEqual(
            {parcel["weight"] for parcel in parcels},
            {"1.00"},
        )

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

    def test_digital_order_gets_a_zero_cost_shipping_rate(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        digital_product = Product.objects.create(
            name="E-book",
            price=Decimal("8.00"),
            digital=True,
        )
        OrderItem.objects.create(order=order, product=digital_product, quantity=1)

        response = self.client.post(
            reverse("shipping_rates"),
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rates"][0]["object_id"], "no_shipping_required")
        self.assertEqual(response.json()["rates"][0]["amount"], "0.00")
        shipping = ShippingModel.objects.get(order=order)
        self.assertEqual(shipping.parcel_manifest, [])
        self.assertEqual(shipping.shippo_shipment_id, "")

    def test_shipping_rates_rejects_invalid_json_empty_cart_and_invalid_parcels(self):
        self.client.force_login(self.user)
        empty_cart = self.client.post(
            reverse("shipping_rates"),
            data="[]",
            content_type="application/json",
        )
        self.assertEqual(empty_cart.status_code, 400)
        self.assertEqual(empty_cart.json()["error"], "Invalid shipping address.")

        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        empty_body = self.client.post(
            reverse("shipping_rates"),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(empty_body.status_code, 400)
        self.assertEqual(empty_body.json()["error"], "Your cart is empty.")

        unpacked = Product.objects.create(name="Unpacked", price=Decimal("10.00"))
        OrderItem.objects.create(order=order, product=unpacked, quantity=1)
        invalid_parcel = self.client.post(
            reverse("shipping_rates"),
            data=json.dumps({
                "name": "Shopper",
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
            }),
            content_type="application/json",
        )
        self.assertEqual(invalid_parcel.status_code, 400)
        self.assertIn("Shipping weight and dimensions are required", invalid_parcel.json()["error"])

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.create_shipment", side_effect=ShippoError("Shippo unavailable."))
    def test_shipping_rate_provider_errors_return_bad_gateway(self, create_shipment):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)

        response = self.client.post(
            reverse("shipping_rates"),
            data=json.dumps({
                "name": "Shopper",
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "Shippo unavailable.")
        create_shipment.assert_called_once()

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.create_transaction")
    def test_staff_can_purchase_shipping_label_and_existing_label_is_reused(
        self, create_transaction
    ):
        staff_user = User.objects.create_superuser(
            username="shipping-admin",
            email="shipping-admin@example.com",
            password="test-password",
        )
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer, complete=True)
        shipping = ShippingModel.objects.create(
            customer=customer,
            order=order,
            selected_rate_id="rate_test",
        )
        create_transaction.return_value = {
            "status": "SUCCESS",
            "tracking_number": "TRACK123",
            "tracking_url_provider": "https://tracking.example/123",
            "label_url": "https://labels.example/123.pdf",
            "provider": "USPS",
        }
        self.client.force_login(staff_user)
        url = reverse("purchase_shipping_label", args=[order.pk])

        response = self.client.post(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tracking_number"], "TRACK123")
        shipping.refresh_from_db()
        self.assertEqual(shipping.label_url, "https://labels.example/123.pdf")
        create_transaction.assert_called_once_with("rate_test")

        create_transaction.reset_mock()
        reused_response = self.client.post(url)
        self.assertEqual(reused_response.status_code, 200)
        self.assertEqual(reused_response.json()["label_url"], "https://labels.example/123.pdf")
        create_transaction.assert_not_called()

    def test_shipping_label_requires_staff_and_a_selected_rate(self):
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer, complete=True)
        self.client.force_login(self.user)

        denied = self.client.post(reverse("purchase_shipping_label", args=[order.pk]))
        self.assertEqual(denied.status_code, 302)

        staff_user = User.objects.create_superuser(
            username="shipping-staff",
            email="shipping-staff@example.com",
            password="test-password",
        )
        self.client.force_login(staff_user)
        no_rate = self.client.post(reverse("purchase_shipping_label", args=[order.pk]))
        self.assertEqual(no_rate.status_code, 400)
        self.assertEqual(
            no_rate.json()["error"],
            "No purchased shipping rate is available for this order.",
        )


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

    @patch.object(OrderAdmin, "message_user")
    @patch("store.admin.ShippoClient.create_transaction")
    def test_order_admin_creates_labels_and_reports_provider_errors(
        self, create_transaction, message_user
    ):
        customer = Customer.objects.create()
        failed_order = Order.objects.create(customer=customer, complete=True)
        failed_shipping = ShippingModel.objects.create(
            customer=customer,
            order=failed_order,
            selected_rate_id="rate_failed",
        )
        completed_order = Order.objects.create(customer=customer, complete=True)
        completed_shipping = ShippingModel.objects.create(
            customer=customer,
            order=completed_order,
            selected_rate_id="rate_success",
        )
        labeled_order = Order.objects.create(customer=customer, complete=True)
        ShippingModel.objects.create(
            customer=customer,
            order=labeled_order,
            selected_rate_id="rate_existing",
            label_url="https://labels.example/existing.pdf",
        )
        Order.objects.create(customer=customer, complete=False)
        create_transaction.side_effect = [
            ShippoError("Rate rejected."),
            {
                "tracking_number": "TRACK456",
                "tracking_url_provider": "https://tracking.example/456",
                "label_url": "https://labels.example/456.pdf",
                "provider": "UPS",
            },
        ]
        order_admin = admin.site._registry[Order]

        order_admin.create_shippo_labels(
            SimpleNamespace(),
            Order.objects.all(),
        )

        failed_shipping.refresh_from_db()
        completed_shipping.refresh_from_db()
        self.assertEqual(failed_shipping.label_url, "")
        self.assertEqual(completed_shipping.label_url, "https://labels.example/456.pdf")
        self.assertEqual(completed_shipping.tracking_number, "TRACK456")
        self.assertEqual(create_transaction.call_count, 2)
        self.assertEqual(message_user.call_count, 2)
        self.assertIn("Rate rejected.", message_user.call_args_list[0].args[1])
        self.assertIn("Created 1 Shippo label(s).", message_user.call_args_list[1].args[1])
