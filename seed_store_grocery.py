from decimal import Decimal
from store.models import GroceryCategory, GroceryItem, StockMovement

# Lines starting with "##" are categories. Item format: name|unit|stock|reorder|cost
DATA = """
## Meat & Seafood
Chicken (with skin)|kg|40|10|220
Mutton (goat meat)|kg|30|8|750
Mutton Liver|kg|5|2|400
Mutton Kudal (intestine)|kg|5|2|300
Goat Blood (Ratha)|kg|3|1|200
Seer Fish (Vanjaram)|kg|15|5|900
Pearl Spot Fish (Karimeen)|kg|10|3|700
Anchovy (Nethili)|kg|10|3|300
Mixed Curry Fish|kg|15|5|400
Eggs|pcs|120|30|7
## Rice & Flour
Basmati Rice|kg|100|25|110
Seeraga Samba Rice|kg|50|15|120
Rice Flour|kg|10|3|60
Corn Flour|kg|10|3|70
Maida|kg|10|3|45
## Vegetables & Herbs
Onion|kg|80|20|40
Tomato|kg|50|15|35
Potato|kg|20|5|35
Ginger|kg|10|3|120
Garlic|kg|10|3|200
Green Chilli|kg|8|2|80
Carrot|kg|10|3|50
Beans|kg|8|2|70
Capsicum|kg|6|2|80
Mushroom|kg|8|3|220
Spring Onion|kg|5|2|80
Mint Leaves|kg|6|2|100
Coriander Leaves|kg|6|2|80
Curry Leaves|kg|3|1|100
Lemon / Lime|pcs|150|40|5
Banana Leaf|pcs|200|50|2
Coconut|pcs|40|10|25
## Dairy
Curd|l|20|5|70
Milk|l|15|5|60
Ghee|kg|10|3|600
Coconut Milk|l|8|2|150
## Spices
Red Chilli Powder|kg|8|2|350
Kashmiri Chilli Powder|kg|4|1|500
Turmeric Powder|kg|4|1|200
Coriander Powder|kg|6|2|150
Garam Masala|kg|3|1|600
Biryani Masala|kg|3|1|700
Black Pepper|kg|3|1|800
Cumin (Jeera)|kg|3|1|400
Fennel Seeds (Sombu)|kg|2|1|300
Mustard Seeds|kg|2|1|120
Dry Red Chilli|kg|4|1|250
Tamarind|kg|5|2|150
Cardamom|g|500|100|3
Cloves|g|500|100|1.2
Cinnamon|g|500|100|0.8
Star Anise|g|300|50|1.5
Bay Leaf|g|300|50|0.4
Salt|kg|25|5|20
## Oils
Sunflower Oil|l|100|25|130
Gingelly Oil|l|10|3|350
Coconut Oil|l|10|3|220
## Dry Fruits
Cashew Nuts|kg|4|1|800
Raisins|kg|2|1|400
Fried Onion (Birista)|kg|5|2|300
## Drinks & Fruits
Sugar|kg|30|8|45
Soda Water|l|60|15|20
Ice Cubes|kg|50|10|10
Apple|kg|10|3|150
Blueberry Syrup|l|5|2|300
Mango Pulp|kg|10|3|150
Orange|kg|10|3|80
Strawberry|kg|5|2|300
Watermelon|kg|15|5|30
Tender Coconut|pcs|30|10|40
## Packaging & Fuel
Takeaway Boxes|pcs|300|100|8
Bucket Biryani Containers|pcs|60|20|40
LPG Cylinder|pcs|5|2|1900
"""

created = 0
cat = None
for line in DATA.strip().splitlines():
    line = line.strip()
    if not line:
        continue
    if line.startswith("## "):
        cat, _ = GroceryCategory.objects.get_or_create(name=line[3:])
        continue
    name, unit, stock, reorder, cost = line.split("|")
    item, is_new = GroceryItem.objects.get_or_create(
        name=name,
        defaults=dict(
            category=cat, unit=unit, current_stock=Decimal(stock),
            reorder_level=Decimal(reorder), cost_per_unit=Decimal(cost), is_active=True,
        ),
    )
    if is_new:
        StockMovement.objects.create(
            item=item, movement_type="IN", change=Decimal(stock),
            stock_after=Decimal(stock), note="Opening stock (seed)",
        )
        created += 1

print("Created:", created, "| Total items now:", GroceryItem.objects.count())
