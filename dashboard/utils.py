from menu.models import FoodItem


def sync_menu_availability(grocery=None):
    """
    Block dishes whose ingredients are low or out of stock.
    Re-open dishes that the system blocked once stock is back.
    Dishes switched off manually by the admin are never touched.
    If grocery is given, only dishes using that item are checked.
    """
    foods = FoodItem.objects.filter(recipe__isnull=False)
    if grocery is not None:
        foods = FoodItem.objects.filter(recipe__grocery=grocery)
    foods = foods.distinct()

    for food in foods:
        ingredients = food.recipe.select_related("grocery")

        can_make = all(
            not r.grocery.is_low and r.grocery.quantity >= r.qty_per_serving
            for r in ingredients
        )

        if not can_make and food.is_available:
            food.is_available = False
            food.auto_disabled = True
            food.save(update_fields=["is_available", "auto_disabled"])
        elif can_make and food.auto_disabled:
            food.is_available = True
            food.auto_disabled = False
            food.save(update_fields=["is_available", "auto_disabled"])