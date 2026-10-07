from django.apps import apps

# name, unit, alert limit
ITEMS = [
    ("Chicken", "kg", 15),
    ("Mutton", "kg", 10),
    ("Mutton liver", "kg", 3),
    ("Mutton kudal", "kg", 3),
    ("Goat blood", "kg", 2),
    ("Fish (curry cut)", "kg", 8),
    ("Pearl spot fish (Kari meen)", "kg", 4),
    ("Anchovy (Nethili)", "kg", 4),
    ("Eggs", "pcs", 60),
    ("Mushroom", "kg", 3),
    ("Mixed vegetables", "kg", 6),
    ("Seeraga samba rice", "kg", 15),
    ("Rice flour", "kg", 3),
    ("Corn flour", "kg", 3),
    ("Ghee", "kg", 3),
    ("Curd", "l", 8),
    ("Onion", "kg", 25),
    ("Tomato", "kg", 12),
    ("Ginger", "kg", 4),
    ("Garlic", "kg", 4),
    ("Green chilli", "kg", 2),
    ("Coriander leaves", "kg", 1),
    ("Mint leaves", "kg", 2),
    ("Curry leaves", "kg", 1),
    ("Lemon", "pcs", 60),
    ("Coconut", "pcs", 20),
    ("Capsicum", "kg", 2),
    ("Spring onion", "kg", 1),
    ("Tamarind", "kg", 2),
    ("Red chilli powder", "kg", 4),
    ("Dry red chilli", "kg", 2),
    ("Turmeric powder", "kg", 1),
    ("Coriander powder", "kg", 3),
    ("Cumin seeds", "kg", 1),
    ("Fennel seeds", "kg", 1),
    ("Mustard seeds", "kg", 1),
    ("Black pepper", "kg", 2),
    ("Garam masala", "kg", 2),
    ("Biryani masala", "kg", 2),
    ("Cardamom", "kg", 0.5),
    ("Cloves", "kg", 0.5),
    ("Cinnamon", "kg", 0.5),
    ("Bay leaf", "kg", 0.5),
    ("Star anise", "kg", 0.5),
    ("Salt", "kg", 15),
    ("Sugar", "kg", 15),
    ("Soy sauce", "l", 3),
    ("Chilli sauce", "l", 3),
    ("Tomato sauce", "l", 3),
    ("Vinegar", "l", 2),
    ("Soda water", "l", 30),
    ("Sugar syrup", "l", 6),
    ("Blueberry crush", "l", 4),
    ("Strawberry crush", "l", 4),
    ("Blueberries", "kg", 2),
    ("Mango pulp", "kg", 6),
    ("Orange", "kg", 12),
    ("Ice cubes", "kg", 30),
]

NEED = set(["name", "quantity", "unit", "low_stock_limit"])
Model = None
for m in apps.get_models():
    names = set()
    for f in m._meta.get_fields():
        names.add(f.name)
    if NEED.issubset(names):
        Model = m
        break

if Model is None:
    raise SystemExit("Could not find the grocery model. Check field names in dashboard/models.py.")

print("Using model:", Model.__name__)

valid_units = set()
for value, label in Model._meta.get_field("unit").choices:
    valid_units.add(value)
print("Valid unit codes:", sorted(valid_units) if valid_units else "any")

created = 0
skipped = 0
bad_unit = []

for name, unit, limit in ITEMS:
    if valid_units and unit not in valid_units:
        bad_unit.append((name, unit))
        continue
    if Model.objects.filter(name__iexact=name).exists():
        skipped += 1
        continue
    Model.objects.create(name=name, unit=unit, quantity=0, low_stock_limit=limit)
    created += 1

print("Created:", created, "| Already existed:", skipped, "| Skipped (bad unit):", len(bad_unit))
for name, unit in bad_unit:
    print("  unit '%s' is not valid for %s" % (unit, name))
if bad_unit:
    print("Fix: change those unit codes in ITEMS to one of the valid codes above, then run again.")
