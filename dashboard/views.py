from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.db.models import Sum
from django.core.mail import send_mail
from django.conf import settings
from .decorators import admin_required
from menu.models import Category, FoodItem, FoodItemImage
from menu.forms import MenuItemForm
from booking.models import Table, TableBooking
from orders.models import Order


@admin_required
def admin_dashboard(request):
    context = {
        'total_menu_items': FoodItem.objects.count(),
        'total_tables': Table.objects.count(),
        'total_bookings': TableBooking.objects.count(),
        'total_users': User.objects.count(),
        'total_orders': Order.objects.count(),
        'total_sales': Order.objects.filter(status='confirmed').aggregate(Sum('total_amount'))['total_amount__sum'] or 0,
        'recent_orders': Order.objects.order_by('-created_at')[:5],
        'recent_bookings': TableBooking.objects.order_by('-created_at')[:5],
    }
    return render(request, 'dashboard/admin_home.html', context)


# ---- Menu ----
@admin_required
def manage_menu(request):
    return render(request, 'dashboard/manage_menu.html', {'items': FoodItem.objects.all()})


@admin_required
def add_menu_item(request):
    form = MenuItemForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        item = form.save()
        for img in request.FILES.getlist('extra_images'):
            FoodItemImage.objects.create(food_item=item, image=img)
        return redirect('dashboard:manage_menu')
    return render(request, 'dashboard/menu_form.html', {'form': form})


@admin_required
def edit_menu_item(request, pk):
    item = get_object_or_404(FoodItem, pk=pk)
    form = MenuItemForm(request.POST or None, request.FILES or None, instance=item)
    if request.method == 'POST' and form.is_valid():
        item = form.save()
        for img in request.FILES.getlist('extra_images'):
            FoodItemImage.objects.create(food_item=item, image=img)
        return redirect('dashboard:manage_menu')
    return render(request, 'dashboard/menu_form.html', {'form': form, 'existing_images': item.gallery_images.all()})


@admin_required
def delete_menu_item(request, pk):
    get_object_or_404(FoodItem, pk=pk).delete()
    return redirect('dashboard:manage_menu')


# ---- Tables ----
@admin_required
def manage_tables(request):
    return render(request, 'dashboard/manage_tables.html', {'tables': Table.objects.all()})


# ---- Bookings ----
@admin_required
def manage_bookings(request):
    bookings = TableBooking.objects.all().order_by('-booking_date')
    return render(request, 'dashboard/manage_bookings.html', {'bookings': bookings})


# ---- Users ----
@admin_required
def manage_users(request):
    return render(request, 'dashboard/manage_users.html', {'users': User.objects.all()})


# ---- Orders ----
@admin_required
def manage_orders(request):
    orders = Order.objects.all().order_by('-created_at')
    return render(request, 'dashboard/manage_orders.html', {'orders': orders})


# ---- Reports ----
@admin_required
def sales_reports(request):
    confirmed_orders = Order.objects.filter(status='confirmed')

    total_orders = confirmed_orders.count()
    total_revenue = confirmed_orders.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    avg_order_value = (total_revenue / total_orders) if total_orders > 0 else 0

    recent_orders = confirmed_orders.order_by('-created_at')[:20]

    context = {
        'total_revenue': total_revenue,
        'total_orders': total_orders,
        'avg_order_value': avg_order_value,
        'recent_orders': recent_orders,
    }
    return render(request, 'dashboard/sales_reports.html', context)


@admin_required
def add_table(request):
    from booking.forms import TableForm
    form = TableForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('dashboard:manage_tables')
    return render(request, 'dashboard/table_form.html', {'form': form})


@admin_required
def edit_table(request, pk):
    from booking.forms import TableForm
    table = get_object_or_404(Table, pk=pk)
    form = TableForm(request.POST or None, instance=table)
    if request.method == 'POST' and form.is_valid():
        form.save()
        return redirect('dashboard:manage_tables')
    return render(request, 'dashboard/table_form.html', {'form': form})


@admin_required
def delete_table(request, pk):
    get_object_or_404(Table, pk=pk).delete()
    return redirect('dashboard:manage_tables')


# ---- QR Codes ----
@admin_required
def table_qr_codes(request):
    return render(request, 'dashboard/table_qr_codes.html', {
        'tables': Table.objects.all().order_by('number')
    })


# ---- Kitchen ----
@admin_required
def kitchen_dashboard(request):
    confirmed_orders = Order.objects.filter(status='confirmed').order_by('created_at')
    preparing_orders = Order.objects.filter(status='preparing').order_by('created_at')
    ready_orders = Order.objects.filter(status='ready').order_by('created_at')

    context = {
        'confirmed_orders': confirmed_orders,
        'preparing_orders': preparing_orders,
        'ready_orders': ready_orders,
    }
    return render(request, 'dashboard/kitchen.html', context)


@admin_required
def update_order_status(request, pk, new_status):
    order = get_object_or_404(Order, pk=pk)
    order.status = new_status
    order.save()

    # --- Send status update email to customer ---
    status_messages = {
        'delivered': 'Your order has been delivered. Enjoy your meal! 🍽️',
    }

    if new_status in status_messages and order.user.email:
        send_mail(
            subject=f'Order #{order.id} - {new_status.capitalize()}',
            message=(
                f"Hi {order.user.get_full_name() or order.user.username},\n\n"
                f"{status_messages[new_status]}\n\n"
                f"Order #{order.id}\n"
                f"Status: {new_status.capitalize()}\n\n"
                f"Thank you for choosing Chandru Restaurant!"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[order.user.email],
            fail_silently=False,
        )

    return redirect('dashboard:kitchen_dashboard')
