import logging
import smtplib

from django.contrib import admin
from django.contrib import messages
from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.urls import reverse

from .models import Vendor
from .tokens import INVITE_SALT


logger = logging.getLogger(__name__)


def send_vendor_invitation(vendor, request):
    token = signing.dumps(
        {"vendor_id": vendor.pk, "email": vendor.email},
        salt=INVITE_SALT,
    )
    invite_url = request.build_absolute_uri(
        reverse("inventory:vendor_invite", args=[token])
    )
    try:
        sent = send_mail(
            subject="Complete your vendor account setup",
            message=(
                f"Hello {vendor.full_name},\n\n"
                f"{vendor.shop_name} has been registered as a vendor. "
                "Use the link below within 7 days to set up your account:\n\n"
                f"{invite_url}\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[vendor.email],
            fail_silently=False,
        )
    except (OSError, smtplib.SMTPException):
        logger.exception("Could not send vendor invitation for vendor %s", vendor.pk)
        return False
    return bool(sent)


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("shop_name", "full_name", "email", "account_status")
    search_fields = ("shop_name", "full_name", "email")
    readonly_fields = ("account_status", "created_at")
    exclude = ("user",)
    actions = ("resend_invitations",)

    @admin.display(description="Account status")
    def account_status(self, vendor):
        return "Active" if vendor.user_id else "Invitation pending"

    def save_model(self, request, obj, form, change):
        is_new = not change
        super().save_model(request, obj, form, change)
        if is_new and not obj.user_id:
            if send_vendor_invitation(obj, request):
                self.message_user(
                    request,
                    f"Vendor invitation sent to {obj.email}.",
                    level=messages.SUCCESS,
                )
            else:
                self.message_user(
                    request,
                    f"Vendor was saved, but the invitation to {obj.email} could not be sent. "
                    "Use the resend invitation action after checking email settings.",
                    level=messages.ERROR,
                )

    @admin.action(description="Resend invitation to selected pending vendors")
    def resend_invitations(self, request, queryset):
        sent_count = 0
        failed_count = 0
        for vendor in queryset.filter(user__isnull=True):
            if send_vendor_invitation(vendor, request):
                sent_count += 1
            else:
                failed_count += 1
        if sent_count:
            self.message_user(
                request,
                f"Sent {sent_count} vendor invitation(s).",
                level=messages.SUCCESS,
            )
        if failed_count:
            self.message_user(
                request,
                f"Could not send {failed_count} invitation(s). Check the email configuration.",
                level=messages.ERROR,
            )
