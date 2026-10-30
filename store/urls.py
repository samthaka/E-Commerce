from django.urls import path
from . import views

urlpatterns = [
    path('', views.store, name="store"),
    path('cart/', views.cart, name="cart"),
    path('checkout/', views.checkout, name="checkout"),
    path('updateitem/', views.updateItem, name="updateitem"),
    path('process_order/', views.processOrder, name="processOrder"),
    path('payments/create-intent/', views.create_payment_intent, name="create_payment_intent"),
    path('shipping/rates/', views.shipping_rates, name="shipping_rates"),
    path('search/', views.search, name='search'),  # Store page with search functionality
    path('accounts/profile/', views.account_profile, name='account_profile'),
    path('orders/', views.order_history, name='order_history'),
    path('orders/mark-delivered/', views.mark_order_delivered, name='mark_order_delivered'),
    path('orders/review/', views.submit_review, name='submit_review'),
    path('orders/<int:order_id>/return/rates/', views.create_return_rates, name='return_rates'),
    path(
        'orders/<int:order_id>/return/<int:return_id>/label/',
        views.purchase_return_label,
        name='purchase_return_label',
    ),
    path(
        'orders/<int:order_id>/shipping-label/',
        views.purchase_shipping_label,
        name='purchase_shipping_label',
    ),
    path(
        'orders/<int:order_id>/tracking/',
        views.refresh_tracking,
        name='refresh_tracking',
    ),

]