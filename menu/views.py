# menu/views.py
from django.core.paginator import Paginator
from django.db.models import Avg, Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from booking.models import Table
from store.models import sold_out_food_ids

from .models import Category, FoodItem


def home(request):
    return render(request, 'menu/home.html')


def qr_welcome(request):
    # Customer scans the table QR -> orders.views.select_table saves the table in
    # the session and sends them here -> short welcome animation -> home page.
    table_no = None
    table_id = request.session.get('selected_table')
    if table_id:
        table = Table.objects.filter(id=table_id).first()
        if table:
            table_no = table.number
    return render(request, 'menu/qr_welcome.html', {'table_no': table_no})


def menu_list(request):
    # Dishes switched on by the admin are all shown.
    # Dishes whose ingredient is at or below its limit get item.sold_out = True,
    # the template shows a "Sold out" badge and disables the Add button.
    # They go back to normal by themselves once the stock is added again.
    sold_out_ids = set(sold_out_food_ids())

    visible_items = FoodItem.objects.orderable()

    categories = list(
        Category.objects.prefetch_related(
            Prefetch('items', queryset=visible_items, to_attr='filtered_items')
        )
    )

    for category in categories:
        for item in category.filtered_items:
            item.sold_out = item.id in sold_out_ids

    context = {
        'categories': categories,
    }
    return render(request, 'menu/menu_list.html', context)


def item_detail(request, item_id):
    # A sold-out dish still opens (no 404), but item.sold_out = True so the
    # template can show the badge and disable the Add button.
    item = get_object_or_404(FoodItem.objects.orderable(), id=item_id)
    item.sold_out = item.id in set(sold_out_food_ids())

    reviews = item.reviews.all().order_by('-created_at')
    avg = reviews.aggregate(Avg('rating'))['rating__avg']
    review_count = reviews.count()

    paginator = Paginator(reviews, 5)
    page_obj = paginator.get_page(request.GET.get('page'))

    related_items = (
        FoodItem.objects.orderable()
        .in_stock()
        .filter(category=item.category)
        .exclude(id=item.id)[:4]
    )

    context = {
        'item': item,
        'page_obj': page_obj,
        'review_count': review_count,
        'avg_rating': round(avg, 1) if avg else 0,
        'related_items': related_items,
    }
    return render(request, 'menu/item_detail.html', context)


# ---- Navbar search (JSON) ----
def search_suggest(request):
    q = request.GET.get("q", "").strip()

    if not q:
        cats = Category.objects.all()[:12]
        items = []
    else:
        cats = Category.objects.filter(name__icontains=q)[:6]
        items = (
            FoodItem.objects.orderable()
            .in_stock()
            .select_related("category")
            .filter(
                Q(name__icontains=q)
                | Q(category__name__icontains=q)
                | Q(description__icontains=q)
            )
            .distinct()[:15]
        )

    return JsonResponse({
        "categories": [{"id": c.id, "name": c.name} for c in cats],
        "items": [
            {
                "id": i.id,
                "name": i.name,
                "category": i.category.name,
                "price": f"{i.price:.2f}",
                "image": i.image.url if i.image else "",
                "is_veg": i.is_veg,
                "url": reverse("item_detail", args=[i.id]),
            }
            for i in items
        ],
    })