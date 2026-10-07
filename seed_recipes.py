from decimal import Decimal

from dashboard.models import GroceryItem, RecipeIngredient
from dashboard.utils import sync_menu_availability
from menu.models import FoodItem

# Set to False if you do not want this script to touch low_stock_limit values.
LOWER_SPICE_LIMITS = True


def scale(items, times):
    return [(name, str(Decimal(qty) * times)) for name, qty in items]


BIRYANI_BASE = [
    ("Onion", "0.100"), ("Tomato", "0.050"), ("Ginger", "0.010"), ("Garlic", "0.010"),
    ("Curd", "0.050"), ("Ghee", "0.020"), ("oil", "0.030"), ("Biryani masala", "0.010"),
    ("Mint leaves", "0.010"), ("Salt", "0.005"), ("Green chilli", "0.010"),
]

FISH_GRAVY_BASE = [
    ("Onion", "0.100"), ("Tomato", "0.100"), ("Red chilli powder", "0.010"),
    ("Coriander powder", "0.010"), ("Turmeric powder", "0.002"), ("oil", "0.030"),
    ("Curry leaves", "0.005"), ("Salt", "0.005"),
]

MUTTON_FRY_BASE = [
    ("Onion", "0.100"), ("Ginger", "0.010"), ("Garlic", "0.010"),
    ("Red chilli powder", "0.010"), ("Turmeric powder", "0.002"), ("oil", "0.040"),
    ("Curry leaves", "0.005"), ("Salt", "0.005"),
]

MOJITO_BASE = [
    ("Mint leaves", "0.010"), ("Lemon", "0.500"), ("Sugar syrup", "0.030"),
    ("Soda water", "0.150"), ("Ice cubes", "0.100"),
]

RECIPES = {
    # Biryani
    "Chicken Biriyani": [("basmathi rice", "0.250"), ("Chicken", "0.200")] + BIRYANI_BASE,
    "Chettinad Chicken Biryani": [("Seeraga samba rice", "0.250"), ("Chicken", "0.200"),
                                  ("Black pepper", "0.005"), ("Star anise", "0.002")] + BIRYANI_BASE,
    "Chettinad Mutton Biryani": [("Seeraga samba rice", "0.250"), ("Mutton", "0.200"),
                                 ("Black pepper", "0.005"), ("Star anise", "0.002")] + BIRYANI_BASE,
    "Hyderabadi Biryani": [("basmathi rice", "0.250"), ("Chicken", "0.200")] + BIRYANI_BASE,
    "Kolkata Briyani": [("basmathi rice", "0.250"), ("Chicken", "0.150"), ("Eggs", "1.000")] + BIRYANI_BASE,
    "Donne Briyani": [("Seeraga samba rice", "0.250"), ("Chicken", "0.200")] + BIRYANI_BASE,
    "Mushroom Biryani": [("basmathi rice", "0.250"), ("Mushroom", "0.150")] + BIRYANI_BASE,
    "Vegetable Biryani": [("basmathi rice", "0.250"), ("Mixed vegetables", "0.150")] + BIRYANI_BASE,
    # Bucket biryani is about 4 servings
    "Bucket Briyani": scale([("basmathi rice", "0.250"), ("Chicken", "0.200")] + BIRYANI_BASE, 4),

    # Fish
    "Fish Curries": [("Fish (curry cut)", "0.250"), ("Tamarind", "0.020")] + FISH_GRAVY_BASE,
    "Meen Kuzhambu": [("Fish (curry cut)", "0.250"), ("Tamarind", "0.030")] + FISH_GRAVY_BASE,
    "Kari Meen": [("Pearl spot fish (Kari meen)", "0.250"), ("Coconut", "0.250"),
                  ("Tamarind", "0.010")] + FISH_GRAVY_BASE,
    "Fish Fry": [("Fish (curry cut)", "0.250"), ("Red chilli powder", "0.010"),
                 ("Turmeric powder", "0.002"), ("Ginger", "0.010"), ("Garlic", "0.010"),
                 ("Lemon", "0.500"), ("Rice flour", "0.020"), ("oil", "0.050"), ("Salt", "0.005")],
    "Meen Varuval": [("Fish (curry cut)", "0.250"), ("Red chilli powder", "0.010"),
                     ("Turmeric powder", "0.002"), ("Ginger", "0.010"), ("Garlic", "0.010"),
                     ("oil", "0.050"), ("Curry leaves", "0.005"), ("Salt", "0.005")],
    "Grilled Roasted Fish": [("Fish (curry cut)", "0.300"), ("Lemon", "1.000"), ("Garlic", "0.010"),
                             ("Red chilli powder", "0.010"), ("oil", "0.020"), ("Salt", "0.005")],
    "Meen Kola Urundai": [("Fish (curry cut)", "0.200"), ("Onion", "0.050"), ("Coconut", "0.250"),
                          ("Fennel seeds", "0.005"), ("Ginger", "0.010"), ("Curry leaves", "0.005"),
                          ("oil", "0.050"), ("Salt", "0.005")],
    "Nethili 65": [("Anchovy (Nethili)", "0.200"), ("Corn flour", "0.030"), ("Rice flour", "0.020"),
                   ("Red chilli powder", "0.010"), ("Curry leaves", "0.005"), ("oil", "0.050"),
                   ("Salt", "0.005")],

    # Mutton
    "Mutton Chukka": [("Mutton", "0.250"), ("Black pepper", "0.005"), ("Fennel seeds", "0.003")] + MUTTON_FRY_BASE,
    "Mutton Pepper Fry": [("Mutton", "0.250"), ("Black pepper", "0.010")] + MUTTON_FRY_BASE,
    "Mutton Uppu Kari": [("Mutton", "0.250"), ("Dry red chilli", "0.010")] + MUTTON_FRY_BASE,
    "Mutton Kudal Fry": [("Mutton kudal", "0.250")] + MUTTON_FRY_BASE,
    "Mutton Liver Fry": [("Mutton liver", "0.200")] + MUTTON_FRY_BASE,
    "Mutton Ratha Poriyal": [("Goat blood", "0.200"), ("Coconut", "0.100")] + MUTTON_FRY_BASE,
    "Mutton Manchuriyan": [("Mutton", "0.200"), ("Corn flour", "0.030"), ("Soy sauce", "0.010"),
                           ("Chilli sauce", "0.010"), ("Tomato sauce", "0.010"),
                           ("Spring onion", "0.020"), ("Capsicum", "0.030"), ("Garlic", "0.010"),
                           ("oil", "0.040"), ("Salt", "0.005")],

    # Juice and mojito
    "Mojito": list(MOJITO_BASE),
    "Apple Mojito": [("Apple", "0.100")] + MOJITO_BASE,
    "Blueberry Mojito": [("Blueberry crush", "0.040"), ("Blueberries", "0.020")] + MOJITO_BASE,
    "Coconut Mojito": [("Coconut", "0.250")] + MOJITO_BASE,
    "Mango Mojito": [("Mango pulp", "0.080")] + MOJITO_BASE,
    "Orange Mojito": [("Orange", "0.150")] + MOJITO_BASE,
    "Strawberry Mojito": [("Strawberry crush", "0.040")] + MOJITO_BASE,
    # No watermelon item exists in the grocery list, so only the base is linked
    "Watermelon Mojito": list(MOJITO_BASE),
    "Apple": [("Apple", "0.300"), ("Sugar", "0.010"), ("Ice cubes", "0.050")],
}

# Spices and herbs are used in grams, so a 5 kg low-stock limit would block every dish.
SMALL_QTY_ITEMS = [
    "Bay leaf", "Black pepper", "Cardamom", "Cinnamon", "Cloves", "Cumin seeds",
    "Fennel seeds", "Mustard seeds", "Star anise", "Turmeric powder", "Garam masala",
    "Biryani masala", "Coriander powder", "Red chilli powder", "Dry red chilli",
    "Curry leaves", "Coriander leaves", "Mint leaves", "Salt",
]


def seed_recipes():
    created = updated = 0
    missing_dishes = set()
    missing_groceries = set()

    for dish_name, ingredients in RECIPES.items():
        food = FoodItem.objects.filter(name=dish_name).first()
        if food is None:
            missing_dishes.add(dish_name)
            continue
        for grocery_name, qty in ingredients:
            grocery = GroceryItem.objects.filter(name=grocery_name).first()
            if grocery is None:
                missing_groceries.add(grocery_name)
                continue
            _, was_created = RecipeIngredient.objects.update_or_create(
                food_item=food,
                grocery=grocery,
                defaults={"qty_per_serving": Decimal(qty)},
            )
            if was_created:
                created += 1
            else:
                updated += 1

    print(f"Recipe rows created: {created}, updated: {updated}")
    if missing_dishes:
        print("Dishes not found:", sorted(missing_dishes))
    if missing_groceries:
        print("Grocery items not found:", sorted(missing_groceries))


def lower_small_limits():
    changed = []
    for name in SMALL_QTY_ITEMS:
        grocery = GroceryItem.objects.filter(name=name).first()
        if grocery and grocery.low_stock_limit == Decimal("5.00"):
            grocery.low_stock_limit = Decimal("0.50")
            grocery.save()
            changed.append(name)
    print("Low stock limit set to 0.50 for:", changed)


seed_recipes()
if LOWER_SPICE_LIMITS:
    lower_small_limits()

sync_menu_availability()
blocked = list(FoodItem.objects.filter(auto_disabled=True).values_list("name", flat=True))
print("Dishes auto-blocked right now:", blocked)
