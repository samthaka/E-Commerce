from django.urls import path

from . import views


app_name = "inventory"

urlpatterns = [
    path("invite/<str:token>/", views.vendor_invite, name="vendor_invite"),
    path(
        "invite/<str:token>/google/complete/",
        views.vendor_google_complete,
        name="vendor_google_complete",
    ),
    path("", views.dashboard, name="dashboard"),
    path("products/add/", views.product_create, name="product_create"),
    path("products/<int:product_id>/edit/", views.product_update, name="product_update"),
    path("products/<int:product_id>/delete/", views.product_delete, name="product_delete"),
]
