from django.shortcuts import render
from django.http import JsonResponse
from .models import *
import json
import datetime
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

    context = {'items': items, 'order': order, 'cartItems': cartItems, 'email': email}
    return render(req, 'store/checkout.html', context)


# Update cart items
def updateItem(req):
    data = json.loads(req.body)
    productId = data['productId']
    action = data['action']

    if req.user.is_authenticated:
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
        return JsonResponse('User is not logged in.', status=401)


def processOrder(req):
    transaction_id = datetime.datetime.now().timestamp()
    data = json.loads(req.body)
    if req.user.is_authenticated:
        customer, created = Customer.objects.get_or_create(user=req.user)  # Ensure customer exists
        order, created = Order.objects.get_or_create(customer=customer, complete=False)
        total = float(data['form']['total'])
        order.transaction_id = transaction_id

        if total == order.get_cart_total:
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
    else:
        print('User is not logged in.')

    return JsonResponse('Payment complete.', safe=False)

# search view for products and categories

def search(request):
    query = request.GET.get('query', '')
    products = Product.objects.all()

    if query:
        # Filter products by name or category name
        products = Product.objects.filter(name__icontains=query) | \
                   Product.objects.filter(category__name__icontains=query)
    
    context = {
        'products': products,
        'query': query,
    }
    return render(request, 'store.html', context)
