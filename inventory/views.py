from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from allauth.account.models import EmailAddress
from store.models import Product

from .forms import VendorPasswordSetupForm, VendorProductForm
from .models import Vendor
from .tokens import INVITE_SALT


INVITE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def confirm_vendor_email(user, email):
    email_address, _ = EmailAddress.objects.get_or_create(
        user=user,
        email=email,
        defaults={"verified": True},
    )
    updates = []
    if not email_address.verified:
        email_address.verified = True
        updates.append("verified")
    if not EmailAddress.objects.filter(user=user, primary=True).exclude(
        pk=email_address.pk
    ).exists() and not email_address.primary:
        email_address.primary = True
        updates.append("primary")
    if updates:
        email_address.save(update_fields=updates)


def invited_vendor(token):
    try:
        payload = signing.loads(
            token,
            salt=INVITE_SALT,
            max_age=INVITE_MAX_AGE_SECONDS,
        )
        return Vendor.objects.get(
            pk=payload["vendor_id"],
            email=payload["email"],
            user__isnull=True,
        )
    except (signing.BadSignature, KeyError, TypeError, Vendor.DoesNotExist):
        return None


def vendor_invite(request, token):
    vendor = invited_vendor(token)
    if vendor is None:
        return render(request, "inventory/invite_invalid.html", status=400)

    User = get_user_model()
    existing_user = User._default_manager.filter(email__iexact=vendor.email).first()
    form = VendorPasswordSetupForm(request.POST or None)
    if request.method == "POST" and existing_user is None and form.is_valid():
        username = vendor.email[:150]
        base_username = username
        suffix = 1
        while User._default_manager.filter(username=username).exists():
            suffix += 1
            username = f"{base_username[:140]}-{suffix}"

        with transaction.atomic():
            user = User._default_manager.create_user(
                username=username,
                email=vendor.email,
                password=form.cleaned_data["password"],
                first_name=vendor.full_name[:150],
            )
            confirm_vendor_email(user, vendor.email)
            vendor.user = user
            vendor.save(update_fields=["user"])
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, "Your vendor account is ready.")
        return redirect("inventory:dashboard")

    google_complete_url = request.build_absolute_uri(
        reverse("inventory:vendor_google_complete", args=[token])
    )
    return render(
        request,
        "inventory/vendor_invite.html",
        {
            "vendor": vendor,
            "form": form,
            "existing_account": existing_user is not None,
            "google_complete_url": google_complete_url,
        },
    )


@login_required
def vendor_google_complete(request, token):
    vendor = invited_vendor(token)
    if vendor is None:
        return render(request, "inventory/invite_invalid.html", status=400)
    if request.user.is_staff or request.user.is_superuser:
        raise PermissionDenied("Administrator accounts cannot be used as vendor accounts.")
    if request.user.email.casefold() != vendor.email.casefold():
        raise PermissionDenied("Sign in with the email address that received this invitation.")

    with transaction.atomic():
        if request.user.email != vendor.email:
            request.user.email = vendor.email
            request.user.save(update_fields=["email"])
        confirm_vendor_email(request.user, vendor.email)
        vendor.user = request.user
        vendor.save(update_fields=["user"])
    messages.success(request, "Your vendor account is ready.")
    return redirect("inventory:dashboard")


def get_vendor(user):
    try:
        return user.vendor_profile
    except Vendor.DoesNotExist:
        raise PermissionDenied("A vendor account is required to manage inventory.")


@login_required
def dashboard(request):
    vendor = get_vendor(request.user)
    products = Product.objects.filter(vendor=vendor).select_related("category")
    return render(
        request,
        "inventory/dashboard.html",
        {"vendor": vendor, "products": products},
    )


@login_required
def product_create(request):
    vendor = get_vendor(request.user)
    form = VendorProductForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        product = form.save(commit=False)
        product.vendor = vendor
        product.save()
        messages.success(request, "Product added to your shop.")
        return redirect("inventory:dashboard")
    return render(
        request,
        "inventory/product_form.html",
        {"vendor": vendor, "form": form, "title": "Add product"},
    )


@login_required
def product_update(request, product_id):
    vendor = get_vendor(request.user)
    product = get_object_or_404(Product, pk=product_id, vendor=vendor)
    form = VendorProductForm(request.POST or None, request.FILES or None, instance=product)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Product updated.")
        return redirect("inventory:dashboard")
    return render(
        request,
        "inventory/product_form.html",
        {"vendor": vendor, "form": form, "title": "Edit product"},
    )


@login_required
def product_delete(request, product_id):
    vendor = get_vendor(request.user)
    product = get_object_or_404(Product, pk=product_id, vendor=vendor)
    if request.method == "POST":
        product.delete()
        messages.success(request, "Product deleted.")
        return redirect("inventory:dashboard")
    return render(
        request,
        "inventory/product_confirm_delete.html",
        {"vendor": vendor, "product": product},
    )
