from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from store.models import Product


class VendorProductForm(forms.ModelForm):
    sku = forms.CharField(max_length=64)

    class Meta:
        model = Product
        fields = (
            "name",
            "sku",
            "description",
            "category",
            "price",
            "stock_quantity",
            "image",
            "digital",
            "shipping_weight",
            "shipping_length",
            "shipping_width",
            "shipping_height",
            "gender",
            "base_colour",
            "season",
            "year",
            "usage",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class VendorPasswordSetupForm(forms.Form):
    password = forms.CharField(
        label="Create password",
        widget=forms.PasswordInput,
    )
    password_confirmation = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput,
    )

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirmation = cleaned_data.get("password_confirmation")
        if password and confirmation and password != confirmation:
            self.add_error("password_confirmation", "The passwords do not match.")
        if password:
            try:
                validate_password(password)
            except ValidationError as error:
                self.add_error("password", error)
        return cleaned_data
