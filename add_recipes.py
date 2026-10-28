from decimal import Decimal
from django.db import transaction
from menu.models import FoodItem
from store.models import GroceryItem, RecipeIngredient

# Quantities are PER 1 PORTION, in the ingredient's own unit (kg, l, pcs, g).
# These are starting estimates. Adjust them later from the Recipes page.

BIRYANI_BASE = {
    "Onion": 0.08, "Tomato": 0.05, "Ginger": 0.01, "Garlic": 0.01,
    "Curd": 0.03, "Biryani Masala": 0.01, "Ghee": 0.01, "Sunflower Oil": 0.02,
    "Mint Leaves": 0.01, "Coriander Leaves": 0.01, "Salt": 0.005,
    "Green Chilli": 0.005, "Bay Leaf": 1, "Cardamom": 1, "Cloves": 1,
    "Cinnamon": 1,
}

MOJITO_BASE = {
    "Mint Leaves": 0.01, "Lemon / Lime": 0.5, "Sugar": 0.02,
    "Soda Water": 0.15, "Ice Cubes": 0.1,
}

MASALA_BASE = {
    "Onion": 0.06, "Ginger": 0.008, "Garlic": 0.008, "Salt": 0.004,
    "Turmeric Powder": 0.002, "Red Chilli Powder": 0.005,
    "Sunflower Oil": 0.02, "Curry Leaves": 0.003,
}


def merge(*parts):
    out = {}
    for p in parts:
        out.update(p)
    return out


# dish pk -> (dish name for display, {grocery name: quantity})
RECIPES = {
    2: ("Chicken Biriyani", merge(BIRYANI_BASE, {"Chicken (with skin)": 0.25, "Basmati Rice": 0.15})),
    8: ("Chettinad Chicken Biryani", merge(BIRYANI_BASE, {"Chicken (with skin)": 0.25, "Seeraga Samba Rice": 0.15, "Black Pepper": 0.003, "Fennel Seeds (Sombu)": 0.002, "Dry Red Chilli": 0.005})),
    3: ("Donne Chicken Briyani", merge(BIRYANI_BASE, {"Chicken (with skin)": 0.25, "Seeraga Samba Rice": 0.15, "Green Chilli": 0.01})),
    11: ("Hyderabadi Chicken Biryani", merge(BIRYANI_BASE, {"Chicken (with skin)": 0.25, "Basmati Rice": 0.15, "Fried Onion (Birista)": 0.02})),
    28: ("Kolkata Chicken Briyani", merge(BIRYANI_BASE, {"Chicken (with skin)": 0.25, "Basmati Rice": 0.15, "Potato": 0.08, "Eggs": 1})),
    7: ("Bucket Briyani", merge({k: v * 4 for k, v in BIRYANI_BASE.items()}, {"Chicken (with skin)": 1.0, "Basmati Rice": 0.6, "Bucket Biryani Containers": 1})),
    18: ("Chettinad Mutton Biryani", merge(BIRYANI_BASE, {"Mutton (goat meat)": 0.25, "Seeraga Samba Rice": 0.15, "Black Pepper": 0.003, "Fennel Seeds (Sombu)": 0.002})),
    9: ("Mushroom Biryani", merge(BIRYANI_BASE, {"Mushroom": 0.15, "Basmati Rice": 0.15})),
    10: ("Vegetable Biryani", merge(BIRYANI_BASE, {"Carrot": 0.04, "Beans": 0.04, "Potato": 0.05, "Capsicum": 0.03, "Basmati Rice": 0.15})),

    1: ("Fish Fry", merge(MASALA_BASE, {"Seer Fish (Vanjaram)": 0.2, "Lemon / Lime": 0.25, "Rice Flour": 0.01})),
    12: ("Meen Varuval", merge(MASALA_BASE, {"Seer Fish (Vanjaram)": 0.2, "Rice Flour": 0.01, "Lemon / Lime": 0.25})),
    13: ("Meen Kuzhambu", merge(MASALA_BASE, {"Mixed Curry Fish": 0.2, "Tamarind": 0.02, "Coconut Milk": 0.05, "Mustard Seeds": 0.002, "Tomato": 0.05})),
    14: ("Nethili 65", merge(MASALA_BASE, {"Anchovy (Nethili)": 0.15, "Corn Flour": 0.02, "Rice Flour": 0.01, "Lemon / Lime": 0.25})),
    15: ("Meen Kola Urundai", merge(MASALA_BASE, {"Mixed Curry Fish": 0.2, "Eggs": 0.5, "Corn Flour": 0.01})),
    16: ("Kari Meen", merge(MASALA_BASE, {"Pearl Spot Fish (Karimeen)": 0.3, "Banana Leaf": 1, "Tamarind": 0.01})),
    31: ("Grilled Roasted Fish", merge(MASALA_BASE, {"Seer Fish (Vanjaram)": 0.25, "Lemon / Lime": 0.5, "Banana Leaf": 1})),
    32: ("Fish Curries", merge(MASALA_BASE, {"Mixed Curry Fish": 0.2, "Tamarind": 0.02, "Tomato": 0.05, "Coconut Milk": 0.05})),

    17: ("Mutton Chukka", merge(MASALA_BASE, {"Mutton (goat meat)": 0.25, "Black Pepper": 0.003, "Coriander Powder": 0.005, "Garam Masala": 0.003})),
    19: ("Mutton Manchuriyan", merge(MASALA_BASE, {"Mutton (goat meat)": 0.25, "Corn Flour": 0.02, "Spring Onion": 0.02, "Capsicum": 0.03})),
    20: ("Mutton Ratha Poriyal", merge(MASALA_BASE, {"Goat Blood (Ratha)": 0.2, "Coconut": 0.1, "Black Pepper": 0.002})),
    21: ("Mutton Kudal Fry", merge(MASALA_BASE, {"Mutton Kudal (intestine)": 0.25, "Black Pepper": 0.004, "Fennel Seeds (Sombu)": 0.002})),
    27: ("Mutton Liver Fry", merge(MASALA_BASE, {"Mutton Liver": 0.2, "Black Pepper": 0.004})),
    34: ("Mutton Pepper Fry", merge(MASALA_BASE, {"Mutton (goat meat)": 0.25, "Black Pepper": 0.008})),
    33: ("Mutton Uppu Kari", merge(MASALA_BASE, {"Mutton (goat meat)": 0.25, "Dry Red Chilli": 0.005, "Black Pepper": 0.003})),

    4: ("Mojito", dict(MOJITO_BASE)),
    24: ("Blueberry Mojito", merge(MOJITO_BASE, {"Blueberry Syrup": 0.03})),
    23: ("Mango Mojito", merge(MOJITO_BASE, {"Mango Pulp": 0.08})),
    26: ("Orange Mojito", merge(MOJITO_BASE, {"Orange": 0.1})),
    25: ("Strawberry Mojito", merge(MOJITO_BASE, {"Strawberry": 0.06})),
    22: ("Watermelon Mojito", merge(MOJITO_BASE, {"Watermelon": 0.15})),
    29: ("Coconut Mojito", merge(MOJITO_BASE, {"Tender Coconut": 0.5})),
    30: ("Apple Mojito", merge(MOJITO_BASE, {"Apple": 0.1})),
}

added = skipped = missing = 0
no_recipe = []

with transaction.atomic():
    for pk, (label, lines) in RECIPES.items():
        try:
            dish = FoodItem.objects.get(pk=pk)
        except FoodItem.DoesNotExist:
            print("MISSING DISH pk=%s (%s)" % (pk, label))
            missing += 1
            continue
        if RecipeIngredient.objects.filter(food_item=dish).exists():
            print("SKIP  %s (already has a recipe)" % dish.name)
            skipped += 1
            continue
        for gname, qty in lines.items():
            try:
                g = GroceryItem.objects.get(name=gname)
            except GroceryItem.DoesNotExist:
                print("MISSING GROCERY '%s' for %s" % (gname, dish.name))
                missing += 1
                continue
            RecipeIngredient.objects.create(
                food_item=dish, ingredient=g, quantity=Decimal(str(qty))
            )
        print("ADDED %s (%d lines)" % (dish.name, len(lines)))
        added += 1

    for f in FoodItem.objects.all().order_by("name"):
        if not RecipeIngredient.objects.filter(food_item=f).exists():
            no_recipe.append(f.name)

print("")
print("Dishes added: %d, skipped: %d, missing items: %d" % (added, skipped, missing))
print("Dishes still without a recipe (add from the Recipes page):")
for n in no_recipe:
    print("  - " + n)
