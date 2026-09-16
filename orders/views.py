import json
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from menu.models import FoodItem
from booking.models import Table
from .models import Order, OrderItem, Coupon


@require_POST
def add_to_cart(request, item_id):
    quantity = int(request.POST.get('quantity', 1))
    if quantity < 1:
        quantity = 1

    if not FoodItem.objects.filter(id=item_id, is_available=True).exists():
        messages.error(request, 'This item is not available.')
        return redirect('menu_list')

    cart = request.session.get('cart', {})
    item_id_str = str(item_id)

    if item_id_str in cart:
        cart[item_id_str] += quantity
    else:
        cart[item_id_str] = quantity

    request.session['cart'] = cart
    request.session.modified = True

    messages.success(request, 'Item added to cart!')
    return redirect('orders:view_cart')


@require_POST
@login_required
def add_to_order(request, item_id):
    quantity = int(request.POST.get('quantity', 1))
    if quantity < 1:
        quantity = 1

    if not FoodItem.objects.filter(id=item_id, is_available=True).exists():
        messages.error(request, 'This item is not available.')
        return redirect('menu_detail', item_id=item_id)

    cart = request.session.get('cart', {})
    item_id_str = str(item_id)

    if item_id_str in cart:
        cart[item_id_str] += quantity
    else:
        cart[item_id_str] = quantity

    request.session['cart'] = cart
    request.session.modified = True

    return redirect('orders:confirm_order')


@require_POST
def bulk_add_to_cart(request):
    try:
        data = json.loads(request.body)
        items = data.get('items', [])
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({'success': False, 'error': 'Invalid data'}, status=400)

    if not items:
        return JsonResponse({'success': False, 'error': 'No items selected'}, status=400)

    cart = request.session.get('cart', {})

    added_count = 0
    for entry in items:
        item_id = str(entry.get('id'))
        quantity = int(entry.get('quantity', 1))

        if not FoodItem.objects.filter(id=item_id, is_available=True).exists():
            continue

        if item_id in cart:
            cart[item_id] += quantity
        else:
            cart[item_id] = quantity

        added_count += 1

    request.session['cart'] = cart
    request.session.modified = True

    return JsonResponse({
        'success': True,
        'added_count': added_count,
        'cart_total_items': sum(cart.values())
    })


def select_table(request, table_id):
    """QR code scan lands here. Stores the table in the session and
    sends the customer to the menu to start ordering for that table."""
    table = get_object_or_404(Table, id=table_id)
    request.session['selected_table'] = table.id
    request.session.modified = True
    messages.success(request, f'Table #{table.number} selected. Add items and checkout!')
    return redirect('menu_list')


def _get_cart_totals(request):
    """Shared helper: computes subtotal, discount and final total for the
    current session cart + any applied coupon. Used by view_cart and confirm_order
    so both stay in sync."""
    cart = request.session.get('cart', {})
    cart_items = []
    subtotal = 0

    for item_id_str, quantity in cart.items():
        try:
            food_item = FoodItem.objects.get(id=item_id_str, is_available=True)
        except FoodItem.DoesNotExist:
            continue

        item_subtotal = food_item.price * quantity
        subtotal += item_subtotal

        cart_items.append({
            'id': food_item.id,
            'name': food_item.name,
            'price': food_item.price,
            'quantity': quantity,
            'subtotal': item_subtotal,
            'image': food_item.image if hasattr(food_item, 'image') else None,
        })

    coupon = None
    discount_amount = 0
    coupon_code = request.session.get('coupon_code')
    if coupon_code:
        coupon = Coupon.objects.filter(code__iexact=coupon_code).first()
        if coupon and coupon.is_valid():
            discount_amount = (subtotal * coupon.discount_percent) / 100
        else:
            # Coupon expired/used up/deactivated since it was applied — drop it silently
            coupon = None
            request.session.pop('coupon_code', None)
            request.session.modified = True

    total = subtotal - discount_amount
    return cart_items, subtotal, coupon, discount_amount, total


def view_cart(request):
    cart_items, subtotal, coupon, discount_amount, total = _get_cart_totals(request)

    context = {
        'cart_items': cart_items,
        'subtotal': subtotal,
        'coupon': coupon,
        'discount_amount': discount_amount,
        'total': total,
        'tables': Table.objects.all().order_by('number'),
        'selected_table_id': request.session.get('selected_table'),
    }
    return render(request, 'orders/cart.html', context)


@require_POST
def apply_coupon(request):
    code = request.POST.get('coupon_code', '').strip()

    if not code:
        messages.error(request, 'Enter a coupon code.')
        return redirect('orders:view_cart')

    coupon = Coupon.objects.filter(code__iexact=code).first()

    if not coupon or not coupon.is_valid():
        messages.error(request, 'Invalid or expired coupon code.')
        return redirect('orders:view_cart')

    request.session['coupon_code'] = coupon.code
    request.session.modified = True
    messages.success(request, f'Coupon "{coupon.code}" applied — {coupon.discount_percent}% off!')
    return redirect('orders:view_cart')


@require_POST
def remove_coupon(request):
    request.session.pop('coupon_code', None)
    request.session.modified = True
    messages.success(request, 'Coupon removed.')
    return redirect('orders:view_cart')


@require_POST
def remove_from_cart(request, item_id):
    cart = request.session.get('cart', {})
    item_id_str = str(item_id)

    if item_id_str in cart:
        del cart[item_id_str]
        request.session['cart'] = cart
        request.session.modified = True
        messages.success(request, 'Item removed from cart.')

    return redirect('orders:view_cart')


@login_required
def confirm_order(request):
    """Creates the order as PENDING and sends the user to the mock
    Razorpay checkout page instead of confirming it immediately."""
    cart = request.session.get('cart', {})

    if not cart:
        messages.error(request, 'Your cart is empty.')
        return redirect('orders:view_cart')

    order_items_data = []
    for item_id_str, quantity in cart.items():
        try:
            food_item = FoodItem.objects.get(id=item_id_str, is_available=True)
        except FoodItem.DoesNotExist:
            continue
        order_items_data.append((food_item, quantity, food_item.price))

    if not order_items_data:
        messages.error(request, 'No valid items in your cart.')
        return redirect('orders:view_cart')

    _, subtotal, coupon, discount_amount, total = _get_cart_totals(request)

    table_id = request.POST.get('table') or request.session.get('selected_table')
    selected_table = None
    if table_id:
        selected_table = Table.objects.filter(id=table_id).first()

    order = Order.objects.create(
        user=request.user,
        table=selected_table,
        status='pending',
        coupon=coupon,
        discount_amount=discount_amount,
        total_amount=total,
    )

    for food_item, quantity, price in order_items_data:
        OrderItem.objects.create(
            order=order,
            food_item=food_item,
            quantity=quantity,
            price=price,
        )

    if coupon:
        coupon.times_used += 1
        coupon.save()

    request.session['cart'] = {}
    request.session.pop('selected_table', None)
    request.session.pop('coupon_code', None)
    request.session.modified = True

    return redirect('orders:initiate_payment', order_id=order.id)


@login_required
def initiate_payment(request, order_id):
    """Shows the mock Razorpay checkout page for a pending order."""
    order = get_object_or_404(Order, id=order_id, user=request.user, status='pending')
    return render(request, 'orders/razorpay_checkout.html', {'order': order})


@login_required
@require_POST
def payment_success(request, order_id):
    """Called when the mock checkout 'completes' — marks the order confirmed."""
    order = get_object_or_404(Order, id=order_id, user=request.user)
    order.status = 'confirmed'
    order.save()

    if request.user.email:
        item_lines = "\n".join(
            f"- {oi.food_item.name} x{oi.quantity} = Rs.{oi.price * oi.quantity}"
            for oi in order.items.all()
        )
        discount_line = f"\nDiscount ({order.coupon.code}): -Rs.{order.discount_amount}\n" if order.coupon else ""
        send_mail(
            subject=f'Order Confirmed - Order #{order.id}',
            message=(
                f"Hi {request.user.get_full_name() or request.user.username},\n\n"
                f"Your order #{order.id} has been confirmed!\n\n"
                f"{item_lines}\n"
                f"{discount_line}\n"
                f"Total: Rs.{order.total_amount}\n\n"
                f"Thank you for ordering with Chandru Restaurant!"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[request.user.email],
            fail_silently=False,
        )

    messages.success(request, 'Your order has been placed successfully!')
    return redirect('orders:order_history')


@login_required
def order_history(request):
    orders = Order.objects.filter(user=request.user).prefetch_related('items')
    return render(request, 'orders/order_history.html', {'orders': orders})