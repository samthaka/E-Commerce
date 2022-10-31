from django.shortcuts import redirect, render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from .models import *
import json
import datetime
from decimal import Decimal
from django.contrib.auth.models import User
from .forms import SearchForm


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
    if req.user.is_authenticated:
        email = req.user.email
        customer, created = Customer.objects.get_or_create(user=req.user)  # Ensure customer exists
        order, created = Order.objects.get_or_create(customer=customer, complete=False)
        items = order.orderitem_set.all()
        cartItems = order.get_cart_items
    else:
        email = None
        items = []  # For non-authenticated users
        order = {'get_cart_total': 0, 'get_cart_items': 0}  # For non-authenticated users
        cartItems = order['get_cart_items']

    if cartItems < 1:
        return redirect('cart')

    context = {'items': items, 'order': order, 'cartItems': cartItems, 'email': email}
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

    data = json.loads(req.body)
    customer, created = Customer.objects.get_or_create(user=req.user)
    order, created = Order.objects.get_or_create(customer=customer, complete=False)

    if order.get_cart_items < 1:
        return JsonResponse('Cannot checkout with an empty cart.', status=400, safe=False)

    total = Decimal(str(data['form']['total']))

    if total != order.get_cart_total:
        return JsonResponse('Order total does not match.', status=400, safe=False)

    order.transaction_id = datetime.datetime.now().timestamp()
    order.complete = True
    order.save()

    ShippingModel.objects.create(
        customer=customer,
        order=order,
        address=data['shipping']['address'],
        city=data['shipping']['city'],
        state=data['shipping']['state'],
        zipcode=data['shipping']['zipcode'],
    )

    return JsonResponse('Payment complete.', safe=False)

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
    ).prefetch_related('orderitem_set__product').order_by('-date_ordered')
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
