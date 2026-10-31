import csv
import io
import json
import zipfile
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from unittest.mock import MagicMock, patch

import stripe

from .admin import OrderAdmin, ProductAdmin
from .forms import SearchForm
from .models import Category, Customer, Order, OrderItem, Product, ProductReview, ReturnShipment, ShippingModel
from .management.commands.import_products import Command
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


class ProductImportCommandTests(TestCase):
    @patch("store.management.commands.import_products.sys.stdin")
    @patch("builtins.input", return_value="yes")
    def test_confirm_reads_from_standard_input(self, input_mock, stdin_mock):
        stdin_mock.isatty.return_value = True

        self.assertTrue(Command()._confirm("Continue?"))
        input_mock.assert_called_once_with()

    @patch("store.management.commands.import_products.sys.stdin")
    def test_confirm_rejects_noninteractive_input(self, stdin_mock):
        stdin_mock.isatty.return_value = False

        with self.assertRaisesMessage(CommandError, "interactive terminal"):
            Command()._confirm("Continue?")

    def test_downloads_products_archive_from_google_drive(self):
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("products.csv", "id,name\n1,Test\n")

        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {
            "Content-Type": "application/zip",
            "Content-Length": str(len(archive_bytes.getvalue())),
        }
        response.iter_content.return_value = [archive_bytes.getvalue()]

        with TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "products.zip"
            command = Command()
            command.stdout = io.StringIO()
            with patch(
                "store.management.commands.import_products.requests.get",
                return_value=response,
            ) as get:
                command._download_products_archive(archive_path)

            get.assert_called_once()
            self.assertEqual(
                get.call_args.kwargs["params"]["id"],
                "1OGwCSCrk877KSqZCbyST9YSd2jrOfYYq",
            )
            self.assertTrue(zipfile.is_zipfile(archive_path))
            self.assertIn("100%", command.stdout.getvalue())
            self.assertIn("[==============================]", command.stdout.getvalue())

    def test_downloads_products_archive_after_google_drive_confirmation(self):
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("products.csv", "id,name\n1,Test\n")

        warning_page = MagicMock()
        warning_page.__enter__.return_value = warning_page
        warning_page.headers = {"Content-Type": "text/html; charset=utf-8"}
        warning_page.url = "https://drive.usercontent.google.com/download"
        warning_page.text = """
            <form action="https://drive.usercontent.google.com/download" method="get">
              <input type="hidden" name="id" value="1OGwCSCrk877KSqZCbyST9YSd2jrOfYYq">
              <input type="hidden" name="export" value="download">
              <input type="hidden" name="confirm" value="t">
              <input type="hidden" name="uuid" value="confirmation-token">
            </form>
        """

        archive_response = MagicMock()
        archive_response.__enter__.return_value = archive_response
        archive_response.headers = {
            "Content-Type": "application/zip",
            "Content-Length": str(len(archive_bytes.getvalue())),
        }
        archive_response.iter_content.return_value = [archive_bytes.getvalue()]

        with TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "products.zip"
            with patch(
                "store.management.commands.import_products.requests.get",
                side_effect=[warning_page, archive_response],
            ) as get:
                Command()._download_products_archive(archive_path)

            self.assertEqual(get.call_count, 2)
            self.assertEqual(
                get.call_args_list[1].args[0],
                "https://drive.usercontent.google.com/download",
            )
            self.assertEqual(
                get.call_args_list[1].kwargs["params"]["uuid"],
                "confirmation-token",
            )
            self.assertTrue(zipfile.is_zipfile(archive_path))

    @patch("store.management.commands.import_products.shutil.disk_usage")
    def test_download_prompt_includes_archive_size_and_available_storage(self, disk_usage):
        disk_usage.return_value = SimpleNamespace(free=8_000_000_000)

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "products" / "products.csv"
            with (
                patch("store.management.commands.import_products.Path.cwd", return_value=root),
                patch.object(Command, "_confirm", return_value=False) as confirm,
            ):
                with self.assertRaisesMessage(CommandError, f"CSV not found: {csv_path}"):
                    Command()._ensure_products_csv(csv_path)

        prompt = confirm.call_args.args[0]
        self.assertIn("about 2.6 GB", prompt)
        self.assertIn("Available storage", prompt)
        self.assertIn("Extraction requires additional space", prompt)

    @patch("store.management.commands.import_products.shutil.disk_usage")
    def test_download_is_rejected_when_available_storage_is_insufficient(self, disk_usage):
        disk_usage.return_value = SimpleNamespace(free=1_000_000_000)

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "products" / "products.csv"
            with (
                patch("store.management.commands.import_products.Path.cwd", return_value=root),
                patch.object(Command, "_confirm") as confirm,
            ):
                with self.assertRaisesMessage(CommandError, "requires about 2.6 GB"):
                    Command()._ensure_products_csv(csv_path)

        confirm.assert_not_called()

    def test_extracts_wrapped_products_archive_into_csv_directory(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "products.zip"
            csv_path = root / "products" / "products.csv"
            csv_path.parent.mkdir()
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("sample_10k/products.csv", "id,name\n1,Test\n")
                archive.writestr("sample_10k/images/1.jpg", "image")

            Command()._extract_products_archive(archive_path, csv_path)

            self.assertEqual(csv_path.read_text(), "id,name\n1,Test\n")
            self.assertEqual((csv_path.parent / "images" / "1.jpg").read_text(), "image")

    def test_rejects_archive_paths_that_escape_extraction_directory(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "products.zip"
            csv_path = root / "products" / "products.csv"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "unsafe")

            with self.assertRaisesMessage(CommandError, "Unsafe path"):
                Command()._extract_products_archive(archive_path, csv_path)
            self.assertFalse((root / "outside.txt").exists())

    def test_import_references_images_in_media_root_without_copying(self):
        columns = [
            "id", "name", "price", "digital", "image", "description",
            "master_category", "sub_category", "article_type", "gender",
            "base_colour", "season", "year", "usage",
            "shipping_weight", "shipping_length", "shipping_width", "shipping_height",
            "is_synthetic",
        ]
        row = {
            "id": "42",
            "name": "Test product",
            "price": "12.50",
            "digital": "false",
            "master_category": "Clothing",
            "sub_category": "Tops",
            "article_type": "Shirt",
        }

        with TemporaryDirectory() as temp_dir:
            media_root = Path(temp_dir)
            images_dir = media_root / "images"
            images_dir.mkdir()
            (images_dir / "42.jpg").write_bytes(b"test image")
            csv_path = media_root / "products.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=columns)
                writer.writeheader()
                writer.writerow(row)

            Product.objects.create(
                name="Test product",
                price=Decimal("12.50"),
                source_id=42,
                image="products/42.jpg",
            )
            with override_settings(MEDIA_ROOT=media_root):
                call_command("import_products", str(csv_path))

            product = Product.objects.get(source_id=42)
            self.assertEqual(product.image.name, "images/42.jpg")
            self.assertEqual(product.category.full_path, "Clothing > Tops > Shirt")
            self.assertTrue((media_root / product.image.name).is_file())
            self.assertFalse((media_root / "products" / "42.jpg").exists())
            self.assertEqual(Product.objects.filter(source_id=42).count(), 1)


class RemoveImportedProductsOptionTests(TestCase):
    def test_removes_imported_products_but_preserves_other_products_and_image_files(self):
        imported = Product.objects.create(
            name="Imported product",
            price=Decimal("12.50"),
            source_id=42,
            image="images/42.jpg",
        )
        Product.objects.create(
            name="Manually added product",
            price=Decimal("8.50"),
        )

        with TemporaryDirectory() as temp_dir:
            media_root = Path(temp_dir)
            image_path = media_root / "images" / "42.jpg"
            image_path.parent.mkdir()
            image_path.write_bytes(b"test image")

            with override_settings(MEDIA_ROOT=media_root):
                call_command("import_products", "--remove_products")

            self.assertFalse(Product.objects.filter(pk=imported.pk).exists())
            self.assertTrue(Product.objects.filter(name="Manually added product").exists())
            self.assertTrue(image_path.is_file())

    def test_dry_run_does_not_remove_imported_products(self):
        product = Product.objects.create(
            name="Imported product",
            price=Decimal("12.50"),
            source_id=42,
        )
        output = io.StringIO()

        call_command("import_products", "--remove_products", "--dry-run", stdout=output)

        self.assertIn("1 imported products would be removed", output.getvalue())
        self.assertTrue(Product.objects.filter(pk=product.pk).exists())

    def test_import_requires_csv_path_without_remove_option(self):
        with self.assertRaisesMessage(
            CommandError,
            "csv_path is required unless --remove_products is used",
        ):
            call_command("import_products")


class StoreModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="customer", password="secure-pass")
        self.customer = Customer.objects.create(
            user=self.user,
            name="Test Customer",
            email="customer@example.com",
        )
        self.category = Category.objects.create(name="Accessories", slug="accessories")
        self.product = Product.objects.create(
            name="Canvas Bag",
            price=Decimal("12.50"),
            category=self.category,
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
        self.assertEqual(str(review), "Test Customer rated Canvas Bag 5/5")

    def test_empty_reviews_order_shipping_and_item_review_defaults(self):
        self.assertEqual(self.product.average_rating, 0)
        self.assertEqual(self.product.review_count, 0)
        self.assertIsNone(self.product.latest_review)
        self.assertEqual(self.order.get_shipping_total, Decimal("0.00"))
        self.assertEqual(self.order.get_order_total, Decimal("0.00"))

        item = OrderItem.objects.create(order=self.order, product=self.product, quantity=1)
        self.assertIsNone(item.review)

        shipping = ShippingModel.objects.create(
            customer=self.customer,
            order=self.order,
            address="1 Main Street",
            shipping_cost=Decimal("2.50"),
        )
        self.assertEqual(self.order.get_shipping_total, Decimal("2.50"))
        self.assertEqual(self.order.get_order_total, Decimal("15.00"))
        self.assertEqual(str(ReturnShipment.objects.create(shipping=shipping)), f"Return for order {self.order.pk}")

    def test_order_item_review_requires_customer_and_returns_matching_review(self):
        item = OrderItem.objects.create(order=self.order, product=self.product, quantity=1)
        self.order.customer = None
        self.order.save()
        self.assertIsNone(item.review)

        self.order.customer = self.customer
        self.order.save()
        review = ProductReview.objects.create(
            product=self.product,
            customer=self.customer,
            order=self.order,
            rating=4,
        )
        self.assertEqual(item.review, review)


class StoreViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="shopper",
            email="shopper@example.com",
            password="secure-pass",
        )
        self.accessories = Category.objects.create(name="Accessories", slug="accessories")
        self.bags = Category.objects.create(
            name="Bags",
            slug="accessories-bags",
            parent=self.accessories,
        )
        self.bag_category = Category.objects.create(
            name="Backpacks",
            slug="accessories-bags-backpacks",
            parent=self.bags,
        )
        self.watch_category = Category.objects.create(name="Watches", slug="watches")
        self.bag = Product.objects.create(
            name="Canvas Bag",
            price=Decimal("12.50"),
            category=self.bag_category,
            shipping_weight=Decimal("1.00"),
            shipping_length=Decimal("12.00"),
            shipping_width=Decimal("10.00"),
            shipping_height=Decimal("2.00"),
        )
        self.watch = Product.objects.create(
            name="Classic Watch",
            price=Decimal("40.00"),
            category=self.watch_category,
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
        self.assertCountEqual(
            response.context["categories"],
            [self.bag_category, self.watch_category],
        )
        self.assertNotContains(response, 'id="categories"')
        self.assertNotContains(response, 'class="category-browser"')
        self.assertFalse(Customer.objects.exists())
        self.assertContains(response, 'class="product-description" hidden')
        self.assertContains(response, "View")
        self.assertContains(response, f'href="{reverse("categories")}"')
        self.assertEqual(
            response.content.count(
                f'data-description-target="product-description-{self.bag.pk}"'.encode()
            ),
            2,
        )
        self.assertContains(response, 'href="/accounts/google/login/">Sign in</a>')

    def test_store_filters_products_by_category(self):
        response = self.client.get(
            reverse("store"),
            {"category": self.watch_category.slug},
        )

        self.assertEqual(list(response.context["products"]), [self.watch])
        self.assertEqual(response.context["selected_category"], self.watch_category.slug)

    def test_categories_page_lists_product_categories_and_links_to_filtered_store(self):
        response = self.client.get(reverse("categories"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "store/categories.html")
        self.assertEqual(
            list(response.context["categories"]),
            [self.bag_category, self.watch_category],
        )
        self.assertContains(response, "Browse categories")
        self.assertContains(
            response,
            f'href="{reverse("store")}?category=accessories-bags-backpacks">',
        )
        self.assertContains(response, "Accessories &gt; Bags")
        self.assertContains(response, "Backpacks")
        self.assertFalse(Customer.objects.exists())

    def test_account_menu_links_to_profile_and_order_history(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("order_history"))

        self.assertContains(
            response,
            f'href="{reverse("categories")}"',
        )
        self.assertContains(response, f'href="{reverse("account_profile")}">Your account</a>')
        self.assertContains(response, f'href="{reverse("order_history")}">My orders</a>')
        self.assertContains(response, f'href="{reverse("store")}">Shop all</a>')
        self.assertNotContains(response, "More navigation options")
        self.assertNotContains(response, "Contact us")

    def test_account_menu_offers_sign_in_for_anonymous_users(self):
        response = self.client.get(reverse("store"))

        self.assertContains(response, 'aria-label="Account menu"')
        self.assertContains(response, 'href="/accounts/google/login/">Sign in</a>')
        self.assertNotContains(response, f'href="{reverse("order_history")}">My orders</a>')

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

    def test_checkout_deduplicates_saved_addresses_and_marks_digital_orders(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        for address in ("1 Main Street", " 1 MAIN STREET ", "2 Other Street"):
            previous_order = Order.objects.create(customer=customer, complete=True)
            ShippingModel.objects.create(
                customer=customer,
                order=previous_order,
                address=address,
                city="Springfield",
                state="IL",
                zipcode="62701",
            )
        order = Order.objects.create(customer=customer)
        ebook = Product.objects.create(
            name="E-book",
            price=Decimal("8.00"),
            digital=True,
        )
        OrderItem.objects.create(order=order, product=ebook, quantity=1)

        response = self.client.get(reverse("checkout"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["requires_shipping"])
        self.assertEqual(response.context["latest_shipping"].address, "2 Other Street")
        self.assertEqual(
            [address.address for address in response.context["saved_addresses"]],
            [" 1 MAIN STREET "],
        )
        self.assertContains(response, "No shipping required")

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

    @override_settings(STRIPE_SECRET_KEY="sk_test_example")
    def test_order_cannot_use_stale_shipping_quote_after_cart_changes(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        self.add_shipping_quote(order)
        OrderItem.objects.filter(order=order, product=self.bag).update(quantity=2)

        with patch("store.views.StripeClient") as stripe_client:
            stripe_client.return_value.v1.payment_intents.retrieve.return_value = (
                self.payment_intent(order, 1500)
            )
            response = self.client.post(
                reverse("processOrder"),
                data=json.dumps({"payment_intent_id": "pi_test_payment"}),
                content_type="application/json",
            )

        order.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Your cart changed. Please request a new shipping quote.")
        self.assertFalse(order.complete)

    @override_settings(STRIPE_SECRET_KEY="sk_test_example")
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
            "payment_intent_id": "pi_test_payment",
        }

        with patch("store.views.StripeClient") as stripe_client:
            stripe_client.return_value.v1.payment_intents.retrieve.return_value = (
                self.payment_intent(order, 1500)
            )
            response = self.client.post(
                reverse("processOrder"),
                data=json.dumps({"shipping": changed_address, "payment_intent_id": "pi_test_payment"}),
                content_type="application/json",
            )

        order.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"],
            "Your shipping address changed. Please request new rates.",
        )
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

    def test_checkout_rejects_incomplete_addresses_and_unknown_rates(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        self.add_shipping_quote(order)
        url = reverse("create_payment_intent")
        headers = {"content_type": "application/json"}

        responses = (
            self.client.post(url, data="{", **headers),
            self.client.post(url, data=json.dumps([]), **headers),
            self.client.post(url, data=json.dumps({}), **headers),
            self.client.post(url, data=json.dumps({"shipping": None}), **headers),
            self.client.post(url, data=json.dumps({"shipping": {
                "rate_id": "rate_test",
                "name": "",
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
            }}), **headers),
            self.client.post(url, data=json.dumps({"shipping": {
                "rate_id": "rate_test",
                "name": "Shopper",
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "XX",
                "zipcode": "62701",
            }}), **headers),
            self.client.post(url, data=json.dumps({"shipping": {
                "rate_id": "rate_test",
                "name": "Shopper",
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "123",
            }}), **headers),
            self.client.post(url, data=json.dumps({"shipping": {
                "rate_id": "unknown_rate",
                "name": "Shopper",
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
            }}), **headers),
        )

        self.assertEqual([response.status_code for response in responses], [400] * 8)
        self.assertEqual(responses[0].json()["error"], "Invalid order data.")
        self.assertEqual(responses[4].json()["error"], "Enter a street address, city, state, and ZIP code.")
        self.assertEqual(responses[5].json()["error"], "Choose a valid US state.")
        self.assertEqual(responses[6].json()["error"], "Enter a valid US ZIP code.")
        self.assertEqual(responses[7].json()["error"], "Choose a valid shipping rate.")

    def test_checkout_requires_an_order_and_shipping_quote(self):
        self.client.force_login(self.user)
        url = reverse("create_payment_intent")
        payload = json.dumps({"shipping": {"rate_id": "rate_test"}})

        empty = self.client.post(url, data=payload, content_type="application/json")
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["error"], "Cannot checkout with an empty cart.")

        customer = Customer.objects.get(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        missing_quote = self.client.post(url, data=payload, content_type="application/json")
        self.assertEqual(missing_quote.status_code, 400)
        self.assertEqual(
            missing_quote.json()["error"],
            "Get a shipping quote before placing this order.",
        )

    @override_settings(STRIPE_PUBLISHABLE_KEY="", STRIPE_SECRET_KEY="")
    def test_payment_intent_reports_missing_stripe_configuration(self):
        self.client.force_login(self.user)
        url = reverse("create_payment_intent")

        unavailable = self.client.post(url, data="{}", content_type="application/json")
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["error"], "Stripe is not configured.")

    @override_settings(
        STRIPE_PUBLISHABLE_KEY="pk_test_example",
        STRIPE_SECRET_KEY="",
    )
    def test_payment_intent_reports_missing_secret_key(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        self.add_shipping_quote(order)
        payload = {
            "shipping": {
                "rate_id": "rate_test",
                "name": "Shopper",
                "address": "1 Main Street",
                "city": "Springfield",
                "state": "IL",
                "zipcode": "62701",
            },
        }

        response = self.client.post(
            reverse("create_payment_intent"),
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "Stripe is not configured.")

    @override_settings(
        STRIPE_PUBLISHABLE_KEY="pk_test_example",
        STRIPE_SECRET_KEY="sk_test_example",
    )
    def test_checkout_details_rejects_malformed_shipping_after_validation(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        self.add_shipping_quote(order)

        with patch("store.views._shipping_address_error", return_value=None):
            response = self.client.post(
                reverse("create_payment_intent"),
                data=json.dumps({"shipping": {
                    "rate_id": "rate_test",
                    "name": [],
                    "address": "1 Main Street",
                    "city": "Springfield",
                    "state": "IL",
                    "zipcode": "62701",
                }}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"],
            "Order details are incomplete or the cart has changed.",
        )

    @override_settings(
        STRIPE_PUBLISHABLE_KEY="pk_test_example",
        STRIPE_SECRET_KEY="sk_test_example",
    )
    def test_payment_intent_translates_stripe_provider_errors(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        self.add_shipping_quote(order)
        payload = json.dumps({"shipping": {
            "rate_id": "rate_test",
            "name": "Shopper",
            "address": "1 Main Street",
            "city": "Springfield",
            "state": "IL",
            "zipcode": "62701",
        }})

        with patch("store.views.StripeClient") as stripe_client:
            stripe_client.return_value.v1.payment_intents.create.side_effect = ValueError("bad config")
            config_error = self.client.post(
                reverse("create_payment_intent"),
                data=payload,
                content_type="application/json",
            )
            stripe_client.return_value.v1.payment_intents.create.side_effect = stripe.StripeError(
                "provider failure"
            )
            provider_error = self.client.post(
                reverse("create_payment_intent"),
                data=payload,
                content_type="application/json",
            )

        self.assertEqual(config_error.status_code, 503)
        self.assertEqual(config_error.json()["error"], "bad config")
        self.assertEqual(provider_error.status_code, 502)
        self.assertEqual(
            provider_error.json()["error"],
            "Payment service is unavailable. Please try again.",
        )

    @override_settings(STRIPE_SECRET_KEY="sk_test_example")
    def test_process_order_rejects_invalid_ids_and_stripe_errors(self):
        self.client.force_login(self.user)
        url = reverse("processOrder")

        invalid_json = self.client.post(url, data="{", content_type="application/json")
        invalid_id = self.client.post(
            url,
            data=json.dumps({"payment_intent_id": "invalid"}),
            content_type="application/json",
        )
        self.assertEqual(invalid_json.status_code, 400)
        self.assertEqual(invalid_json.json()["error"], "Invalid order data.")
        self.assertEqual(invalid_id.status_code, 400)
        self.assertEqual(
            invalid_id.json()["error"],
            "A valid payment is required to place this order.",
        )

        with patch("store.views.StripeClient") as stripe_client:
            stripe_client.return_value.v1.payment_intents.retrieve.side_effect = ValueError(
                "bad config"
            )
            config_error = self.client.post(
                url,
                data=json.dumps({"payment_intent_id": "pi_test"}),
                content_type="application/json",
            )
            stripe_client.return_value.v1.payment_intents.retrieve.side_effect = stripe.StripeError(
                "provider failure"
            )
            provider_error = self.client.post(
                url,
                data=json.dumps({"payment_intent_id": "pi_test"}),
                content_type="application/json",
            )

        self.assertEqual(config_error.status_code, 503)
        self.assertEqual(config_error.json()["error"], "bad config")
        self.assertEqual(provider_error.status_code, 502)

    @override_settings(STRIPE_SECRET_KEY="sk_test_example")
    def test_process_order_uses_saved_address_and_validates_payment_metadata(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        self.add_shipping_quote(order)
        url = reverse("processOrder")

        with patch("store.views.StripeClient") as stripe_client:
            retrieve = stripe_client.return_value.v1.payment_intents.retrieve
            retrieve.return_value = self.payment_intent(order, 1500)
            response = self.client.post(
                url,
                data=json.dumps({"payment_intent_id": "pi_test_payment"}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

            order.complete = False
            order.save()
            for changes in (
                {"user_id": "another-user"},
                {"amount_received": 0},
                {"currency": "eur"},
                {"order_id": "another-order"},
            ):
                intent = self.payment_intent(order, 1500)
                for name, value in changes.items():
                    if name in ("user_id", "order_id"):
                        setattr(intent.metadata, name, value)
                    else:
                        setattr(intent, name, value)
                retrieve.return_value = intent
                rejected = self.client.post(
                    url,
                    data=json.dumps({"payment_intent_id": "pi_test_payment"}),
                    content_type="application/json",
                )
                self.assertEqual(rejected.status_code, 400)
                self.assertEqual(
                    rejected.json()["error"],
                    "Payment does not match this order."
                    if "user_id" in changes
                    else "Payment is incomplete or does not match this order.",
                )

        order.refresh_from_db()
        self.assertFalse(order.complete)

    @override_settings(STRIPE_SECRET_KEY="sk_test_example")
    def test_process_order_without_shipping_quote_returns_a_validation_error(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)

        with patch("store.views.StripeClient") as stripe_client:
            stripe_client.return_value.v1.payment_intents.retrieve.return_value = (
                self.payment_intent(order, 1500)
            )
            response = self.client.post(
                reverse("processOrder"),
                data=json.dumps({"payment_intent_id": "pi_test_payment"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"],
            "Get a shipping quote before placing this order.",
        )

    def test_mark_order_delivered_requires_a_pending_completed_order(self):
        self.client.force_login(self.user)
        url = reverse("mark_order_delivered")

        invalid_id = self.client.post(
            url,
            data=json.dumps({"order_id": "not-an-id"}),
            content_type="application/json",
        )
        missing_order = self.client.post(
            url,
            data=json.dumps({"order_id": 12345}),
            content_type="application/json",
        )
        self.assertEqual(invalid_id.status_code, 404)
        self.assertEqual(missing_order.status_code, 404)

        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer, complete=True)
        delivered = self.client.post(
            url,
            data=json.dumps({"order_id": str(order.pk)}),
            content_type="application/json",
        )

        self.assertEqual(delivered.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_DELIVERED)

    def test_customers_can_submit_and_update_reviews_for_delivered_orders(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(
            customer=customer,
            complete=True,
            status=Order.STATUS_DELIVERED,
        )
        url = reverse("submit_review")
        invalid_values = (
            {},
            {"product_id": self.bag.pk, "order_id": order.pk, "rating": "invalid"},
            {"product_id": self.bag.pk, "order_id": order.pk, "rating": "6"},
            {"product_id": 9999, "order_id": order.pk, "rating": "5"},
        )
        for values in invalid_values:
            response = self.client.post(url, data=values)
            self.assertEqual(response.status_code, 302)
            self.assertFalse(ProductReview.objects.exists())

        response = self.client.post(url, data={
            "product_id": self.bag.pk,
            "order_id": order.pk,
            "rating": "4",
            "message": "  Great bag.  ",
        })
        review = ProductReview.objects.get(product=self.bag, order=order)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(review.message, "Great bag.")

        self.client.post(url, data={
            "product_id": self.bag.pk,
            "order_id": order.pk,
            "rating": "5",
            "message": "x" * 1100,
        })
        review.refresh_from_db()
        self.assertEqual(review.rating, 5)
        self.assertEqual(len(review.message), 1000)

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.create_shipment")
    def test_return_rate_errors_and_messages_are_reported(self, create_shipment):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        missing = self.client.post(reverse("return_rates", args=[9999]), data="{}",
                                   content_type="application/json")
        self.assertEqual(missing.status_code, 404)

        digital_order = Order.objects.create(customer=customer, complete=True)
        digital = Product.objects.create(name="E-book", price=Decimal("8.00"), digital=True)
        OrderItem.objects.create(order=digital_order, product=digital, quantity=1)
        digital_shipping = self.add_shipping_quote(digital_order)
        no_physical_return = self.client.post(
            reverse("return_rates", args=[digital_order.pk]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(no_physical_return.status_code, 400)
        self.assertEqual(
            no_physical_return.json()["error"],
            "Digital orders do not need a return shipment.",
        )
        self.assertIsNotNone(digital_shipping)

        order_without_shipping = Order.objects.create(customer=customer, complete=True)
        OrderItem.objects.create(order=order_without_shipping, product=self.bag, quantity=1)
        no_address = self.client.post(
            reverse("return_rates", args=[order_without_shipping.pk]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(no_address.status_code, 400)

        order = Order.objects.create(customer=customer, complete=True)
        OrderItem.objects.create(order=order, product=self.bag, quantity=1)
        self.add_shipping_quote(order)
        url = reverse("return_rates", args=[order.pk])
        create_shipment.return_value = {"rates": [], "messages": " Carrier is unavailable. "}
        with self.assertLogs("store.views", level="WARNING") as logged:
            no_rates = self.client.post(url, data="{}", content_type="application/json")
        self.assertEqual(no_rates.status_code, 422)
        self.assertIn("Carrier is unavailable.", logged.output[0])

        create_shipment.return_value = {"rates": []}
        with self.assertLogs("store.views", level="WARNING") as logged:
            no_details = self.client.post(url, data="{}", content_type="application/json")
        self.assertEqual(no_details.status_code, 422)
        self.assertIn("Shippo did not return any return-shipping rates.", logged.output[0])

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.create_shipment")
    def test_return_rates_translate_parcel_and_provider_errors(self, create_shipment):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer, complete=True)
        unpacked = Product.objects.create(name="Unpacked", price=Decimal("10.00"))
        OrderItem.objects.create(order=order, product=unpacked, quantity=1)
        shipping = ShippingModel.objects.create(customer=customer, order=order, address="1 Main Street")

        invalid_parcel = self.client.post(
            reverse("return_rates", args=[order.pk]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(invalid_parcel.status_code, 400)
        self.assertIn("Shipping weight and dimensions are required", invalid_parcel.json()["error"])
        create_shipment.assert_not_called()

        unpacked.shipping_weight = Decimal("1.00")
        unpacked.shipping_length = Decimal("1.00")
        unpacked.shipping_width = Decimal("1.00")
        unpacked.shipping_height = Decimal("1.00")
        unpacked.save()
        create_shipment.side_effect = ShippoError("Return rates unavailable.")
        provider_error = self.client.post(
            reverse("return_rates", args=[order.pk]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(provider_error.status_code, 502)
        self.assertEqual(provider_error.json()["error"], "Return rates unavailable.")
        self.assertIsNotNone(shipping)

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    def test_return_label_validation_errors_and_existing_labels(self):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer, complete=True)
        shipping = self.add_shipping_quote(order)
        return_shipment = ReturnShipment.objects.create(
            shipping=shipping,
            rates=[{
                "object_id": "return_rate",
                "amount": "3.25",
                "provider": "USPS",
                "servicelevel": "Ground",
            }],
        )
        url = reverse("purchase_return_label", args=[order.pk, return_shipment.pk])

        unknown = self.client.post(
            reverse("purchase_return_label", args=[order.pk, 9999]),
            data=json.dumps({"rate_id": "return_rate"}),
            content_type="application/json",
        )
        no_rate = self.client.post(url, data="[]", content_type="application/json")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(no_rate.status_code, 400)

        return_shipment.selected_rate_id = "return_rate"
        return_shipment.tracking_number = "TRACK123"
        return_shipment.label_url = "https://labels.example/return.pdf"
        return_shipment.tracking_url = "https://tracking.example/return"
        return_shipment.save()
        with patch("store.views.ShippoClient.create_transaction") as create_transaction:
            reused = self.client.post(
                url,
                data=json.dumps({"rate_id": "return_rate"}),
                content_type="application/json",
            )
        self.assertEqual(reused.status_code, 200)
        self.assertEqual(reused.json()["tracking_number"], "TRACK123")
        create_transaction.assert_not_called()

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.create_transaction", side_effect=ShippoError("Label unavailable."))
    def test_return_label_translates_provider_errors(self, create_transaction):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer, complete=True)
        shipping = self.add_shipping_quote(order)
        return_shipment = ReturnShipment.objects.create(
            shipping=shipping,
            rates=[{"object_id": "return_rate", "amount": "3.25"}],
        )

        response = self.client.post(
            reverse("purchase_return_label", args=[order.pk, return_shipment.pk]),
            data=json.dumps({"rate_id": "return_rate"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "Label unavailable.")
        create_transaction.assert_called_once_with("return_rate")

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.create_transaction", side_effect=ShippoError("Label unavailable."))
    def test_shipping_label_translates_provider_errors(self, create_transaction):
        staff_user = User.objects.create_superuser(
            username="shipping-error-admin",
            email="shipping-error-admin@example.com",
            password="pass",
        )
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer, complete=True)
        ShippingModel.objects.create(
            customer=customer,
            order=order,
            selected_rate_id="rate_test",
        )
        self.client.force_login(staff_user)

        response = self.client.post(reverse("purchase_shipping_label", args=[order.pk]))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "Label unavailable.")
        create_transaction.assert_called_once_with("rate_test")

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.track")
    def test_tracking_rejects_missing_shipments_and_handles_provider_errors(self, track):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        missing = self.client.post(
            reverse("refresh_tracking", args=[9999]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(missing.status_code, 404)

        order = Order.objects.create(customer=customer, complete=True)
        shipping = self.add_shipping_quote(order)
        missing_return = self.client.post(
            reverse("refresh_tracking", args=[order.pk]),
            data=json.dumps({"return_id": 12345}),
            content_type="application/json",
        )
        self.assertEqual(missing_return.status_code, 404)

        unavailable = self.client.post(
            reverse("refresh_tracking", args=[order.pk]),
            data="[]",
            content_type="application/json",
        )
        self.assertEqual(unavailable.status_code, 400)

        shipping.carrier = "USPS"
        shipping.tracking_number = "TRACK123"
        shipping.save()
        track.side_effect = ShippoError("Tracking unavailable.")
        failed = self.client.post(
            reverse("refresh_tracking", args=[order.pk]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(failed.status_code, 502)
        self.assertEqual(failed.json()["error"], "Tracking unavailable.")

    @override_settings(SHIPPO_API_TOKEN="shippo_test_token", SHIPPO_TEST_MODE=True)
    @patch("store.views.ShippoClient.track", return_value={})
    def test_return_tracking_refresh_preserves_existing_status(self, track):
        self.client.force_login(self.user)
        customer = Customer.objects.create(user=self.user)
        order = Order.objects.create(customer=customer, complete=True)
        shipping = self.add_shipping_quote(order)
        return_shipment = ReturnShipment.objects.create(
            shipping=shipping,
            carrier="USPS",
            tracking_number="RETURN123",
            tracking_status="TRANSIT",
            tracking_status_details="In transit",
        )

        response = self.client.post(
            reverse("refresh_tracking", args=[order.pk]),
            data=json.dumps({"return_id": return_shipment.pk}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "TRANSIT")
        self.assertEqual(response.json()["details"], "In transit")
        track.assert_called_once_with("USPS", "RETURN123")


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
            category=Category.objects.create(name="Test", slug="test"),
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

    @patch.object(OrderAdmin, "message_user")
    def test_order_admin_does_not_report_success_when_no_labels_are_created(self, message_user):
        Order.objects.create(complete=False)

        admin.site._registry[Order].create_shippo_labels(
            SimpleNamespace(),
            Order.objects.all(),
        )

        message_user.assert_not_called()
