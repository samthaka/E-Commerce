from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.views.decorators.http import require_POST
from .models import *
import json
import logging
import datetime
from decimal import Decimal
import re
from .shippo import (
    ShippoClient,
    ShippoError,
    InvalidParcelError,
    find_rate,
    order_parcels,
    origin_address,
    recipient_address,
    serialize_rates,
    transaction_fields,
)
from .forms import SearchForm


logger = logging.getLogger(__name__)


US_STATES = (
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"), ("DC", "District of Columbia"),
)
US_STATE_CODES = {code for code, _ in US_STATES}


def _json_data(req):
    try:
        data = json.loads(req.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _json_error(message, status):
    return JsonResponse({"error": message}, status=status)


def _shipping_address_error(data):
    required_fields = ("name", "address", "city", "state", "zipcode")
    if any(not isinstance(data.get(field), str) or not data[field].strip() for field in required_fields):
        return "Enter a street address, city, state, and ZIP code."
    if data["state"].upper() not in US_STATE_CODES:
        return "Choose a valid US state."
    if not re.fullmatch(r"\d{5}(?:-\d{4})?", data["zipcode"].strip()):
        return "Enter a valid US ZIP code."
    return None


def _shipment_rates_error(shipment, fallback):
    messages = shipment.get("messages")
    if isinstance(messages, str):
        details = [messages]
    elif isinstance(messages, list):
        details = [
            message.get("text") or message.get("message") or str(message)
            if isinstance(message, dict)
            else str(message)
            for message in messages
        ]
    else:
        details = []
    details = [detail.strip() for detail in details if detail.strip()]
    return f"{fallback} {' '.join(details)}" if details else fallback


# Create your views here.

# Store function for rendering store view with category filtering
def store(req):
    if req.user.is_authenticated:
        customer, created = Customer.objects.get_or_create(user=req.user)  # Ensure customer exists
        order, created = Order.objects.get_or_create(customer=customer, complete=False)
        items = order.orderitem_set.all()
        cartItems = order.get_cart_items
    else:
        items = []  # For non-authenticated users
        order = {'get_cart_total': 0, 'get_cart_items': 0}  # For non-authenticated users
        cartItems = order['get_cart_items']

    # Get selected category from the query parameters (if any)
    selected_category = req.GET.get('category', None)

    if selected_category:
        # Filter products by the selected category
        products = Product.objects.filter(category=selected_category)
    else:
        # If no category is selected, show all products
        products = Product.objects.all()

    # Get all unique categories for the hamburger menu
    categories = Product.objects.values_list('category', flat=True).distinct()

    context = {
        'products': products, 
        'cartItems': cartItems,
        'categories': categories,
        'selected_category': selected_category,
    }
    
    return render(req, 'store/store.html', context)


# Cart function for rendering cart view
def cart(req):
    if req.user.is_authenticated:
        customer, created = Customer.objects.get_or_create(user=req.user)  # Ensure customer exists
        order, created = Order.objects.get_or_create(customer=customer, complete=False)
        items = order.orderitem_set.all()
        cartItems = order.get_cart_items
    else:
        items = []  # For non-authenticated users
        order = {'get_cart_total': 0, 'get_cart_items': 0}  # For non-authenticated users
        cartItems = order['get_cart_items']

    context = {'items': items, 'order': order, 'cartItems': cartItems}
    return render(req, 'store/cart.html', context)


# Checkout function for rendering checkout view
def checkout(req):
    saved_addresses = []
    if req.user.is_authenticated:
        email = req.user.email
        customer, created = Customer.objects.get_or_create(user=req.user)  # Ensure customer exists
        order, created = Order.objects.get_or_create(customer=customer, complete=False)
        items = order.orderitem_set.all()
        cartItems = order.get_cart_items
        previous_shipping = (
            ShippingModel.objects.filter(customer=customer, order__complete=True)
            .exclude(address="")
            .select_related("order")
            .order_by("-order__date_ordered", "-date_added")
        )
        seen_addresses = set()
        for shipping in previous_shipping:
            address_key = tuple(
                (value or "").strip().casefold()
                for value in (
                    shipping.address,
                    shipping.city,
                    shipping.state,
                    shipping.zipcode,
                    shipping.country,
                )
            )
            if address_key not in seen_addresses:
                saved_addresses.append(shipping)
                seen_addresses.add(address_key)
    else:
        email = None
        items = []  # For non-authenticated users
        order = {'get_cart_total': 0, 'get_cart_items': 0}  # For non-authenticated users
        cartItems = order['get_cart_items']

    if cartItems < 1:
        return redirect('cart')

    physical_items = [item for item in items if item.product and not item.product.digital]
    context = {
        'items': items,
        'order': order,
        'cartItems': cartItems,
        'email': email,
        'states': US_STATES,
        'requires_shipping': bool(physical_items),
        'latest_shipping': saved_addresses[0] if saved_addresses else None,
        'saved_addresses': saved_addresses[1:],
    }
    return render(req, 'store/checkout.html', context)


# Update cart items
def updateItem(req):
    data = json.loads(req.body)
    productId = data['productId']
    action = data['action']

    if req.user.is_authenticated:
        if action not in ('add', 'remove'):
            return JsonResponse('Invalid action.', status=400, safe=False)

        customer, created = Customer.objects.get_or_create(user=req.user)  # Ensure customer exists
        product = Product.objects.get(id=productId)
        order, created = Order.objects.get_or_create(customer=customer, complete=False)
        orderItem, created = OrderItem.objects.get_or_create(order=order, product=product)

        if action == 'add':
            print("Added")
            orderItem.quantity = (orderItem.quantity + 1)
        elif action == 'remove':
            print("Removed")
            orderItem.quantity = (orderItem.quantity - 1)

        orderItem.save()

        if orderItem.quantity <= 0:
            orderItem.delete()

        return JsonResponse('Item was added.', safe=False)
    else:
        return JsonResponse('User is not logged in.', status=401, safe=False)


def processOrder(req):
    if not req.user.is_authenticated:
        return JsonResponse('User is not logged in.', status=401, safe=False)

    data = _json_data(req)
    if data is None:
        return _json_error("Invalid order data.", 400)
    customer, created = Customer.objects.get_or_create(user=req.user)
    order, created = Order.objects.get_or_create(customer=customer, complete=False)

    if order.get_cart_items < 1:
        return JsonResponse('Cannot checkout with an empty cart.', status=400, safe=False)

    shipping = ShippingModel.objects.filter(order=order).order_by("-date_added").first()
    if shipping is None:
        return _json_error("Get a shipping quote before placing this order.", 400)
    try:
        parcels = order_parcels(order)
        total = Decimal(str(data["form"]["total"]))
        submitted_shipping = data["shipping"]
        selected_rate_id = submitted_shipping["rate_id"]
    except (KeyError, TypeError, ValueError, ShippoError):
        return _json_error("Order details are incomplete or the cart has changed.", 400)
    if parcels != shipping.parcel_manifest:
        return _json_error("Your cart changed. Please request a new shipping quote.", 400)
    if parcels:
        address_error = _shipping_address_error(submitted_shipping)
        if address_error:
            return _json_error(address_error, 400)
        quoted_address = (
            shipping.recipient_name,
            shipping.address,
            shipping.city,
            shipping.state,
            shipping.zipcode,
            shipping.recipient_email,
        )
        requested_address = (
            submitted_shipping["name"].strip(),
            submitted_shipping["address"].strip(),
            submitted_shipping["city"].strip(),
            submitted_shipping["state"].upper(),
            submitted_shipping["zipcode"].strip(),
            req.user.email or submitted_shipping.get("email", ""),
        )
        if requested_address != quoted_address:
            return _json_error("Your shipping address changed. Please request new rates.", 400)

    selected_rate = find_rate(shipping.rates, selected_rate_id)
    if not parcels:
        selected_rate = selected_rate or (
            {"object_id": "no_shipping_required", "amount": "0.00", "provider": "", "servicelevel": ""}
            if selected_rate_id == "no_shipping_required"
            else None
        )
    if selected_rate is None:
        return _json_error("Choose a valid shipping rate.", 400)
    shipping_cost = Decimal(str(selected_rate["amount"]))
    if total != order.get_cart_total + shipping_cost:
        return JsonResponse('Order total does not match.', status=400, safe=False)

    with transaction.atomic():
        order.transaction_id = datetime.datetime.now().timestamp()
        order.complete = True
        order.save()
        shipping.selected_rate_id = selected_rate_id
        shipping.shipping_cost = shipping_cost
        shipping.carrier = selected_rate.get("provider", "")
        shipping.service_level = selected_rate.get("servicelevel", "")
        shipping.save()

    return JsonResponse('Order placed.', safe=False)


@login_required
@require_POST
def shipping_rates(req):
    data = _json_data(req)
    if data is None:
        return _json_error("Invalid shipping address.", 400)

    customer, _ = Customer.objects.get_or_create(user=req.user)
    order = Order.objects.filter(customer=customer, complete=False).first()
    if order is None or order.get_cart_items < 1:
        return _json_error("Your cart is empty.", 400)
    try:
        parcels = order_parcels(order)
        if parcels:
            address_error = _shipping_address_error(data)
            if address_error:
                return _json_error(address_error, 400)
            sender = origin_address()
            recipient = recipient_address(
                data,
                req.user.email or data.get("email", ""),
            )
            shipment = ShippoClient().create_shipment(sender, recipient, parcels)
            rates = serialize_rates(shipment.get("rates", []))
            if not rates:
                logger.warning(
                    "Shippo returned no checkout rates: %s",
                    _shipment_rates_error(
                        shipment,
                        "Shippo did not return any available shipping rates.",
                    ),
                )
                return _json_error(
                    "No shipping rates are available right now. Please try again later.",
                    422,
                )
            shipment_id = shipment.get("object_id", "")
        else:
            rates = [{
                "object_id": "no_shipping_required",
                "amount": "0.00",
                "currency": "USD",
                "provider": "",
                "servicelevel": "No shipping required",
                "estimated_days": None,
                "duration_terms": "",
            }]
            shipment_id = ""
    except InvalidParcelError as exc:
        return _json_error(str(exc), 400)
    except ShippoError as exc:
        return _json_error(str(exc), 502)

    shipping, _ = ShippingModel.objects.update_or_create(
        order=order,
        defaults={
            "customer": customer,
            "address": data.get("address", "").strip(),
            "recipient_name": data.get("name", "").strip(),
            "recipient_email": req.user.email or data.get("email", ""),
            "city": data.get("city", "").strip(),
            "state": data.get("state", "").upper(),
            "zipcode": data.get("zipcode", "").strip(),
            "country": "US",
            "shippo_shipment_id": shipment_id,
            "rates": rates,
            "parcel_manifest": parcels,
            "selected_rate_id": "",
            "shipping_cost": Decimal("0.00"),
            "carrier": "",
            "service_level": "",
        },
    )
    return JsonResponse({"rates": rates, "shipment_id": shipping.shippo_shipment_id})

# search view for products and categories

def search(request):
    query = request.GET.get('query', '')
    products = Product.objects.all()

    if query:
        # Filter products by name or category
        products = Product.objects.filter(name__icontains=query) | \
                   Product.objects.filter(category__icontains=query)
    
    context = {
        'products': products,
        'query': query,
        'cartItems': 0,
        'categories': Product.objects.values_list('category', flat=True).distinct(),
    }
    return render(request, 'store/store.html', context)


@login_required
def account_profile(req):
    customer = Customer.objects.filter(user=req.user).first()
    order = Order.objects.filter(customer=customer, complete=False).first() if customer else None
    context = {
        'cartItems': order.get_cart_items if order else 0,
        'categories': Product.objects.values_list('category', flat=True).distinct(),
    }
    return render(req, 'account/profile.html', context)


@login_required
def order_history(req):
    customer, created = Customer.objects.get_or_create(user=req.user)
    orders = Order.objects.filter(
        customer=customer,
        complete=True,
    ).prefetch_related('orderitem_set__product', 'shippingmodel_set__returns').order_by('-date_ordered')
    sections = [
        {
            'label': label,
            'orders': orders.filter(status=status),
        }
        for status, label in Order.STATUS_CHOICES
    ]
    open_order = Order.objects.filter(customer=customer, complete=False).first()
    context = {
        'sections': sections,
        'cartItems': open_order.get_cart_items if open_order else 0,
        'categories': Product.objects.values_list('category', flat=True).distinct(),
    }
    return render(req, 'store/order_history.html', context)


@login_required
@require_POST
def create_return_rates(req, order_id):
    customer, _ = Customer.objects.get_or_create(user=req.user)
    order = Order.objects.filter(
        pk=order_id,
        customer=customer,
        complete=True,
    ).first()
    if order is None:
        return _json_error("Order not found.", 404)
    shipping = ShippingModel.objects.filter(order=order).order_by("-date_added").first()
    if shipping is None:
        return _json_error("This order has no shipping address.", 400)
    try:
        parcels = order_parcels(order)
        if not parcels:
            return _json_error("Digital orders do not need a return shipment.", 400)
        recipient = {
            "name": shipping.recipient_name or req.user.get_username(),
            "street1": shipping.address,
            "city": shipping.city,
            "state": shipping.state,
            "zip": shipping.zipcode,
            "country": "US",
            "email": shipping.recipient_email or req.user.email,
        }
        shipment = ShippoClient().create_shipment(recipient, origin_address(), parcels)
        rates = serialize_rates(shipment.get("rates", []))
        if not rates:
            logger.warning(
                "Shippo returned no return-shipping rates: %s",
                _shipment_rates_error(
                    shipment,
                    "Shippo did not return any return-shipping rates.",
                ),
            )
            return _json_error(
                "No return-shipping rates are available right now. Please try again later.",
                422,
            )
    except InvalidParcelError as exc:
        return _json_error(str(exc), 400)
    except ShippoError as exc:
        return _json_error(str(exc), 502)

    return_shipment = ReturnShipment.objects.create(
        shipping=shipping,
        shippo_shipment_id=shipment.get("object_id", ""),
        rates=rates,
    )
    return JsonResponse({
        "return_id": return_shipment.pk,
        "rates": rates,
    })


@login_required
@require_POST
def purchase_return_label(req, order_id, return_id):
    customer, _ = Customer.objects.get_or_create(user=req.user)
    return_shipment = ReturnShipment.objects.filter(
        pk=return_id,
        shipping__order__pk=order_id,
        shipping__order__customer=customer,
        shipping__order__complete=True,
    ).first()
    if return_shipment is None:
        return _json_error("Return shipment not found.", 404)
    data = _json_data(req)
    rate_id = data.get("rate_id") if data else None
    rate = find_rate(return_shipment.rates, rate_id)
    if rate is None:
        return _json_error("Choose a valid return-shipping rate.", 400)
    if return_shipment.selected_rate_id:
        return JsonResponse({
            "tracking_number": return_shipment.tracking_number,
            "label_url": return_shipment.label_url,
            "tracking_url": return_shipment.tracking_url,
        })
    try:
        transaction = ShippoClient().create_transaction(rate_id)
    except ShippoError as exc:
        return _json_error(str(exc), 502)
    for field, value in transaction_fields(transaction).items():
        setattr(return_shipment, field, value)
    return_shipment.selected_rate_id = rate_id
    return_shipment.shipping_cost = Decimal(str(rate["amount"]))
    return_shipment.carrier = return_shipment.carrier or rate.get("provider", "")
    return_shipment.service_level = rate.get("servicelevel", "")
    return_shipment.save()
    return JsonResponse({
        "tracking_number": return_shipment.tracking_number,
        "label_url": return_shipment.label_url,
        "tracking_url": return_shipment.tracking_url,
    })


@login_required
@user_passes_test(lambda user: user.is_staff)
@require_POST
def purchase_shipping_label(req, order_id):
    shipping = ShippingModel.objects.filter(
        order__pk=order_id,
        order__complete=True,
    ).order_by("-date_added").first()
    if shipping is None or not shipping.selected_rate_id:
        return _json_error("No purchased shipping rate is available for this order.", 400)
    if shipping.label_url:
        return JsonResponse({"label_url": shipping.label_url, "tracking_number": shipping.tracking_number})
    try:
        transaction = ShippoClient().create_transaction(shipping.selected_rate_id)
    except ShippoError as exc:
        return _json_error(str(exc), 502)
    for field, value in transaction_fields(transaction).items():
        setattr(shipping, field, value)
    shipping.save()
    return JsonResponse({
        "label_url": shipping.label_url,
        "tracking_number": shipping.tracking_number,
    })


@login_required
@require_POST
def refresh_tracking(req, order_id):
    customer, _ = Customer.objects.get_or_create(user=req.user)
    shipping = ShippingModel.objects.filter(
        order__pk=order_id,
        order__customer=customer,
        order__complete=True,
    ).order_by("-date_added").first()
    if shipping is None:
        return _json_error("Order shipment not found.", 404)
    data = _json_data(req) or {}
    return_id = data.get("return_id")
    if return_id:
        shipment = shipping.returns.filter(pk=return_id).first()
        if shipment is None:
            return _json_error("Return shipment not found.", 404)
    else:
        shipment = shipping
    if not shipment.tracking_number or not shipment.carrier:
        return _json_error("Tracking is not available until a shipping label is created.", 400)
    try:
        tracker = ShippoClient().track(shipment.carrier, shipment.tracking_number)
    except ShippoError as exc:
        return _json_error(str(exc), 502)
    tracking_status = tracker.get("tracking_status") or {}
    shipment.tracking_status = tracking_status.get("status") or shipment.tracking_status
    shipment.tracking_status_details = (
        tracking_status.get("status_details") or shipment.tracking_status_details
    )
    shipment.save()
    if shipment is shipping and shipment.tracking_status.lower() == "delivered":
        shipping.order.status = Order.STATUS_DELIVERED
        shipping.order.save(update_fields=["status"])
    return JsonResponse({
        "status": shipment.tracking_status,
        "details": shipment.tracking_status_details,
    })
