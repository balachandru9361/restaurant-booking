from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from menu.models import FoodItem
from .models import Review
from .forms import ReviewForm


@login_required
def add_review(request, item_id):
    item = get_object_or_404(FoodItem, id=item_id)

    
    existing_review = Review.objects.filter(user=request.user, food_item=item).first()

    if request.method == 'POST':
        form = ReviewForm(request.POST, instance=existing_review)
        if form.is_valid():
            review = form.save(commit=False)
            review.user = request.user
            review.food_item = item
            review.save()
            messages.success(request, 'Thank you for your review!')
            return redirect('item_detail', item_id=item.id)
    else:
        form = ReviewForm(instance=existing_review)

    context = {
        'form': form,
        'item': item,
        'reviews': item.reviews.all(),
    }
    return render(request, 'reviews/review_form.html', context)