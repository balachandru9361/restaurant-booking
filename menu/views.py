from django.shortcuts import render, get_object_or_404
from django.db.models import Avg
from django.core.paginator import Paginator
from .models import Category, FoodItem


def home(request):
    return render(request, 'menu/home.html')


def menu_list(request):
    categories = Category.objects.prefetch_related('items').all()

    for category in categories:
        category.filtered_items = category.items.filter(is_available=True)

    context = {
        'categories': categories,
    }
    return render(request, 'menu/menu_list.html', context)


def item_detail(request, item_id):
    item = get_object_or_404(FoodItem, id=item_id, is_available=True)

    reviews = item.reviews.all().order_by('-created_at')
    avg = reviews.aggregate(Avg('rating'))['rating__avg']
    review_count = reviews.count()

    paginator = Paginator(reviews, 5)
    page_obj = paginator.get_page(request.GET.get('page'))

    related_items = FoodItem.objects.filter(
        category=item.category, is_available=True
    ).exclude(id=item.id)[:4]

    context = {
        'item': item,
        'page_obj': page_obj,
        'review_count': review_count,
        'avg_rating': round(avg, 1) if avg else 0,
        'related_items': related_items,
    }
    return render(request, 'menu/item_detail.html', context)