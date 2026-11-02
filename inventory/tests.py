from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import User
from django.core import signing
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from allauth.account.models import EmailAddress
from unittest.mock import patch

from store.models import Customer, Order, Product

from .admin import VendorAdmin
from .models import Vendor
from .tokens import INVITE_SALT


class InventoryViewsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="vendor-user", password="password")
        self.vendor = Vendor.objects.create(
            user=self.user,
            full_name="Vendor User",
            shop_name="Sample Shop",
            email="vendor@example.com",
            shop_address="1 Market Street",
        )
        self.other_user = User.objects.create_user(
            username="other-vendor",
            password="password",
        )
        self.other_vendor = Vendor.objects.create(
            user=self.other_user,
            full_name="Other Vendor",
            shop_name="Other Shop",
            email="other@example.com",
            shop_address="2 Market Street",
        )

    def test_anonymous_vendor_is_redirected_to_login(self):
        response = self.client.get(reverse("inventory:dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("account_login"), response.url)

    def test_regular_user_without_vendor_cannot_open_inventory(self):
        regular_user = User.objects.create_user(
            username="regular-user",
            password="password",
        )
        self.client.force_login(regular_user)

        response = self.client.get(reverse("inventory:dashboard"))

        self.assertEqual(response.status_code, 403)

    def test_vendor_only_sees_and_edits_own_products(self):
        own_product = Product.objects.create(
            vendor=self.vendor,
            name="Own product",
            sku="OWN-001",
            price=Decimal("12.50"),
        )
        other_product = Product.objects.create(
            vendor=self.other_vendor,
            name="Other product",
            sku="OTHER-001",
            price=Decimal("8.50"),
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:dashboard"))
        edit_response = self.client.get(
            reverse("inventory:product_update", args=[other_product.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Own product")
        self.assertNotContains(response, "Other product")
        self.assertEqual(edit_response.status_code, 404)
        self.assertTrue(Product.objects.filter(pk=own_product.pk).exists())

    def test_vendor_can_create_update_and_delete_products(self):
        self.client.force_login(self.user)
        create_response = self.client.post(
            reverse("inventory:product_create"),
            {
                "name": "New product",
                "sku": "NEW-001",
                "description": "A vendor product",
                "price": "19.95",
                "stock_quantity": "6",
                "digital": "",
                "vendor": str(self.other_vendor.pk),
            },
        )

        product = Product.objects.get(sku="NEW-001")
        self.assertRedirects(create_response, reverse("inventory:dashboard"))
        self.assertEqual(product.vendor, self.vendor)
        self.assertEqual(product.stock_quantity, 6)

        update_response = self.client.post(
            reverse("inventory:product_update", args=[product.pk]),
            {
                "name": "Updated product",
                "sku": "NEW-001",
                "description": "Updated details",
                "price": "21.00",
                "stock_quantity": "9",
                "digital": "",
            },
        )
        product.refresh_from_db()
        self.assertRedirects(update_response, reverse("inventory:dashboard"))
        self.assertEqual(product.name, "Updated product")
        self.assertEqual(product.price, Decimal("21.00"))

        delete_response = self.client.post(
            reverse("inventory:product_delete", args=[product.pk])
        )
        self.assertRedirects(delete_response, reverse("inventory:dashboard"))
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())

    def test_vendor_storefront_is_limited_and_cart_is_disabled(self):
        own_product = Product.objects.create(
            vendor=self.vendor,
            name="Own listing",
            sku="OWN-LISTING",
            price=Decimal("10.00"),
        )
        Product.objects.create(
            vendor=self.other_vendor,
            name="Other listing",
            sku="OTHER-LISTING",
            price=Decimal("20.00"),
        )
        self.client.force_login(self.user)

        store_response = self.client.get(reverse("store"))
        search_response = self.client.get(reverse("search"), {"query": "listing"})
        cart_response = self.client.get(reverse("cart"))
        checkout_response = self.client.get(reverse("checkout"))
        add_item_response = self.client.post(
            reverse("updateitem"),
            data='{"productId": %d, "action": "add"}' % own_product.pk,
            content_type="application/json",
        )

        self.assertContains(store_response, "Own listing")
        self.assertNotContains(store_response, "Other listing")
        self.assertNotContains(store_response, "Add to cart")
        self.assertNotContains(store_response, 'class="site-icon-link cart-icon-link"')
        self.assertContains(search_response, "Own listing")
        self.assertNotContains(search_response, "Other listing")
        self.assertRedirects(cart_response, reverse("inventory:dashboard"))
        self.assertRedirects(checkout_response, reverse("inventory:dashboard"))
        self.assertEqual(add_item_response.status_code, 403)
        self.assertFalse(Product.objects.filter(pk=own_product.pk).first().orderitem_set.exists())

    def test_vendor_does_not_create_customer_or_order_when_browsing(self):
        self.client.force_login(self.user)

        self.client.get(reverse("store"))
        self.client.get(reverse("categories"))

        self.assertFalse(Customer.objects.filter(user=self.user).exists())
        self.assertFalse(Order.objects.filter(customer__user=self.user).exists())

    def test_vendor_checkout_apis_are_blocked(self):
        self.client.force_login(self.user)
        json_request = {
            "data": "{}",
            "content_type": "application/json",
        }

        for endpoint in ("shipping_rates", "create_payment_intent", "processOrder"):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(reverse(endpoint), **json_request)
                self.assertEqual(response.status_code, 403)

    def test_vendor_admin_is_registered(self):
        self.assertIn(Vendor, admin.site._registry)
        self.assertIsInstance(admin.site._registry[Vendor], VendorAdmin)


class VendorInvitationTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username="inventory-admin",
            email="admin@example.com",
            password="Admin-test-password-2026",
        )

    def make_token(self, vendor):
        return signing.dumps(
            {"vendor_id": vendor.pk, "email": vendor.email},
            salt=INVITE_SALT,
        )

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_admin_creates_vendor_without_user_and_sends_invitation(self):
        self.client.force_login(self.admin_user)
        add_url = reverse("admin:inventory_vendor_add")
        page = self.client.get(add_url)

        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="user"')
        response = self.client.post(
            add_url,
            {
                "full_name": "New Vendor",
                "shop_name": "New Shop",
                "email": "new-vendor@example.com",
                "shop_address": "10 Main Street",
                "_save": "Save",
            },
        )

        vendor = Vendor.objects.get(email="new-vendor@example.com")
        self.assertIsNone(vendor.user)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(vendor.email, mail.outbox[0].to)
        self.assertIn("http://testserver/inventory/invite/", mail.outbox[0].body)

    def test_invitation_allows_vendor_to_set_password(self):
        vendor = Vendor.objects.create(
            full_name="Invited Vendor",
            shop_name="Invited Shop",
            email="invite@example.com",
            shop_address="12 Main Street",
        )
        token = self.make_token(vendor)

        response = self.client.post(
            reverse("inventory:vendor_invite", args=[token]),
            {
                "password": "Strong-vendor-password-2026!",
                "password_confirmation": "Strong-vendor-password-2026!",
            },
        )

        vendor.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("inventory:dashboard"))
        self.assertIsNotNone(vendor.user)
        self.assertFalse(vendor.user.is_staff)
        self.assertTrue(vendor.user.check_password("Strong-vendor-password-2026!"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), vendor.user_id)
        email_address = EmailAddress.objects.get(user=vendor.user, email=vendor.email)
        self.assertTrue(email_address.verified)
        self.assertTrue(email_address.primary)

    def test_existing_admin_account_is_not_assigned_as_vendor(self):
        admin_account = User.objects.create_superuser(
            username="same-email-admin",
            email="shared@example.com",
            password="Admin-test-password-2026",
        )
        vendor = Vendor.objects.create(
            full_name="Invited Vendor",
            shop_name="Protected Shop",
            email=admin_account.email,
            shop_address="12 Main Street",
        )
        self.client.force_login(admin_account)
        token = self.make_token(vendor)

        response = self.client.post(
            reverse("inventory:vendor_invite", args=[token]),
            {
                "password": "Strong-vendor-password-2026!",
                "password_confirmation": "Strong-vendor-password-2026!",
            },
        )

        vendor.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(vendor.user)

    def test_google_completion_links_only_matching_non_admin_user(self):
        vendor = Vendor.objects.create(
            full_name="Google Vendor",
            shop_name="Google Shop",
            email="google-vendor@example.com",
            shop_address="12 Main Street",
        )
        user = User.objects.create_user(
            username="google-vendor",
            email=vendor.email,
            password="password",
        )
        self.client.force_login(user)

        response = self.client.get(
            reverse(
                "inventory:vendor_google_complete",
                args=[self.make_token(vendor)],
            )
        )

        vendor.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(vendor.user, user)
        email_address = EmailAddress.objects.get(user=user, email=vendor.email)
        self.assertTrue(email_address.verified)

    def test_expired_invitation_is_rejected(self):
        vendor = Vendor.objects.create(
            full_name="Expired Vendor",
            shop_name="Expired Shop",
            email="expired@example.com",
            shop_address="12 Main Street",
        )
        token = self.make_token(vendor)
        with patch(
            "inventory.views.signing.loads",
            side_effect=signing.SignatureExpired,
        ):
            response = self.client.get(reverse("inventory:vendor_invite", args=[token]))

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(vendor.user)
