from django.apps import apps

FoodItem = None
for model in apps.get_models():
    if model.__name__ == 'FoodItem':
        FoodItem = model
        break

if FoodItem is None:
    print("FoodItem model not found.")
else:
    non_veg_keywords = [
        'chicken', 'kozhi', 'mutton', 'lamb', 'beef', 'pork',
        'fish', 'meen', 'prawn', 'shrimp', 'crab', 'squid',
        'egg', 'muttai', 'omelette', 'omelet',
        'seafood', 'liver', 'kaleji', 'meat', 'keema', 'kheema',
        'bacon', 'sausage', 'ham', 'turkey', 'duck', 'quail',
    ]

    all_items = FoodItem.objects.all()
    updated_nonveg = 0
    updated_veg = 0

    for item in all_items:
        name_lower = item.name.lower()
        is_nonveg_match = any(keyword in name_lower for keyword in non_veg_keywords)

        if is_nonveg_match and item.is_veg:
            item.is_veg = False
            item.save()
            updated_nonveg += 1
        elif not is_nonveg_match and not item.is_veg:
            item.is_veg = True
            item.save()
            updated_veg += 1

    print(f"Marked {updated_nonveg} item(s) as Non-Veg.")
    print(f"Marked {updated_veg} item(s) as Veg.")
    print("\n--- Final status of all items (please verify) ---")
    for item in FoodItem.objects.all().order_by('name'):
        status = "Veg" if item.is_veg else "Non-Veg"
        print(f"{item.name:40s} -> {status}")
