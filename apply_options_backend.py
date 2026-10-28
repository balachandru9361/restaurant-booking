import pathlib
import re
import shutil

# ---------------- 1) orders/models.py ----------------
mp = pathlib.Path("orders/models.py")
ms = mp.read_text(encoding="utf-8")
if "options_text" in ms:
    print("orders/models.py already patched")
else:
    old = """    price = models.DecimalField(max_digits=8, decimal_places=2)

    def subtotal(self):"""
    new = """    price = models.DecimalField(max_digits=8, decimal_places=2)
    # Chosen options for this line (for the kitchen). The price above already includes the extras.
    options_text = models.CharField(max_length=500, blank=True)

    def subtotal(self):"""
    if ms.count(old) != 1:
        print("orders/models.py: expected OrderItem block not found. Nothing changed there.")
    else:
        shutil.copy(mp, str(mp) + ".bak_options")
        mp.write_text(ms.replace(old, new, 1), encoding="utf-8")
        print("orders/models.py updated")

# ---------------- 2) orders/views.py ----------------
vp = pathlib.Path("orders/views.py")
vs = vp.read_text(encoding="utf-8")

HELPERS = '''def _parse_line(key):
    """'12' -> (12, []), '12:5-7' -> (12, [5, 7]). A cart line is a dish plus its chosen options."""
    item_part, _, option_part = str(key).partition(':')
    try:
        item_id = int(item_part)
    except ValueError:
        return None, []
    option_ids = [int(p) for p in option_part.split('-') if p.isdigit()]
    return item_id, option_ids


def _line_key(item_id, option_ids):
    ids = sorted(set(option_ids))
    if not ids:
        return str(item_id)
    return str(item_id) + ':' + '-'.join(str(i) for i in ids)


def _load_options(food_item, option_ids):
    """Option rows for a cart line. Only options that still belong to the dish and are available."""
    if not option_ids:
        return []
    found = {
        o.id: o
        for o in Option.objects.filter(
            id__in=option_ids, group__food_item=food_item, is_available=True
        )
    }
    return [found[i] for i in option_ids if i in found]


def _validate_options(food_item, raw_ids):
    """Checks the chosen options against the dish's option groups.
    Returns (clean_option_ids, error_message). The message is empty when all is fine."""
    chosen = set()
    for raw in raw_ids:
        try:
            chosen.add(int(raw))
        except (TypeError, ValueError):
            return [], 'Invalid option.'

    clean = []
    known = set()
    for group in food_item.option_groups.prefetch_related('options'):
        group_options = [o for o in group.options.all() if o.is_available]
        group_ids = {o.id for o in group_options}
        known |= group_ids
        picked = sorted(chosen & group_ids)

        if group.group_type == 'single' and len(picked) > 1:
            return [], 'Pick only one for ' + group.title + '.'
        if group.group_type == 'multi' and group.max_select and len(picked) > group.max_select:
            return [], 'Pick at most ' + str(group.max_select) + ' for ' + group.title + '.'
        if group.is_required and group_options and not picked:
            return [], 'Please choose an option for ' + group.title + '.'
        clean.extend(picked)

    if chosen - known:
        return [], 'One of the chosen options is not available.'
    return clean, ''


'''

ADD_OLD = """    cart = request.session.get('cart', {})
    _add_to_session_cart(cart, item_id, quantity)

    request.session['cart'] = cart
    request.session.modified = True

    # AJAX (menu page side panel): stay on the same page, return JSON
"""
ADD_NEW = """    # Chosen options (Choose Your Side, Add-ons ...). Empty for plain dishes.
    food_item = FoodItem.objects.get(id=item_id)
    option_ids, option_error = _validate_options(food_item, request.POST.getlist('options'))
    if option_error:
        if _is_ajax(request):
            return JsonResponse({'success': False, 'error': option_error}, status=400)
        messages.error(request, option_error)
        return redirect('menu_list')

    cart = request.session.get('cart', {})
    _add_to_session_cart(cart, _line_key(item_id, option_ids), quantity)

    request.session['cart'] = cart
    request.session.modified = True

    # AJAX (menu page side panel): stay on the same page, return JSON
"""

BULK_OLD = """        if not FoodItem.objects.filter(id=item_id, is_available=True).exists():
            continue

        _add_to_session_cart(cart, item_id, quantity)
        added_count += 1
"""
BULK_NEW = """        food_item = FoodItem.objects.filter(id=item_id, is_available=True).first()
        if food_item is None:
            continue

        # A dish that needs a choice (required option group) cannot be bulk added
        _, option_error = _validate_options(food_item, [])
        if option_error:
            continue

        _add_to_session_cart(cart, item_id, quantity)
        added_count += 1
"""

TOTALS_OLD = """    for item_id_str, quantity in cart.items():
        try:
            food_item = FoodItem.objects.get(id=item_id_str, is_available=True)
        except (FoodItem.DoesNotExist, ValueError):
            continue

        item_subtotal = food_item.price * quantity
        subtotal += item_subtotal

        cart_items.append({
            'id': food_item.id,
            'name': food_item.name,
            'price': food_item.price,
            'quantity': quantity,
            'subtotal': item_subtotal,
            'image': food_item.image if hasattr(food_item, 'image') else None,
            'image_url': food_item.image.url if food_item.image else '',
            'is_veg': food_item.is_veg,
        })
"""
TOTALS_NEW = """    for line_key, quantity in cart.items():
        item_id, option_ids = _parse_line(line_key)
        if item_id is None:
            continue
        try:
            food_item = FoodItem.objects.get(id=item_id, is_available=True)
        except FoodItem.DoesNotExist:
            continue

        options = _load_options(food_item, option_ids)
        unit_price = food_item.price + sum((o.extra_price for o in options), Decimal('0.00'))
        item_subtotal = unit_price * quantity
        subtotal += item_subtotal

        cart_items.append({
            'id': food_item.id,
            'line': line_key,
            'name': food_item.name,
            'price': unit_price,
            'quantity': quantity,
            'subtotal': item_subtotal,
            'image': food_item.image if hasattr(food_item, 'image') else None,
            'image_url': food_item.image.url if food_item.image else '',
            'is_veg': food_item.is_veg,
            'options_text': ', '.join(o.name for o in options),
        })
"""

ITEM_OPTIONS_VIEW = '''def item_options(request, item_id):
    """Option groups of one dish as JSON. The menu popup calls this when ADD is clicked."""
    food_item = get_object_or_404(FoodItem, id=item_id, is_available=True)
    groups = []
    for group in food_item.option_groups.prefetch_related('options'):
        options = [
            {
                'id': o.id,
                'name': o.name,
                'extra_price': _money(o.extra_price),
                'image_url': o.image.url if o.image else '',
                'is_default': o.is_default,
            }
            for o in group.options.all() if o.is_available
        ]
        if not options:
            continue
        groups.append({
            'id': group.id,
            'title': group.title,
            'type': group.group_type,
            'required': group.is_required,
            'max_select': group.max_select,
            'options': options,
        })
    return JsonResponse({'success': True, 'groups': groups})


'''

CONFIRM_OLD = """    order_items_data = []
    for item_id_str, quantity in cart.items():
        try:
            food_item = FoodItem.objects.get(id=item_id_str, is_available=True)
        except (FoodItem.DoesNotExist, ValueError):
            continue
        order_items_data.append((food_item, quantity, food_item.price))
"""
CONFIRM_NEW = """    order_items_data = []
    cart_lines, _a, _b, _c, _d = _get_cart_totals(request)
    for line in cart_lines:
        order_items_data.append((
            FoodItem.objects.get(id=line['id']),
            line['quantity'],
            line['price'],
            line['options_text'],
        ))
"""

CREATE_OLD = """        for food_item, quantity, price in order_items_data:
            OrderItem.objects.create(
                order=order,
                food_item=food_item,
                quantity=quantity,
                price=price,
            )
"""
CREATE_NEW = """        for food_item, quantity, price, options_text in order_items_data:
            OrderItem.objects.create(
                order=order,
                food_item=food_item,
                quantity=quantity,
                price=price,
                options_text=options_text,
            )
"""

NEW_UPDATE = '''@require_POST
def update_cart_qty(request, item_id):
    """+ / - buttons on the cart page and the menu side panel. Returns JSON so the page updates live."""
    cart = request.session.get('cart', {})
    # 'line' picks one cart line when the same dish was added with different options
    key = request.POST.get('line') or str(item_id)

    if key not in cart:
        return JsonResponse({'success': False, 'error': 'Item not in cart'}, status=404)

    action = request.POST.get('action')
    if action == 'inc':
        cart[key] = min(cart[key] + 1, MAX_QTY)
    elif action == 'dec':
        if cart[key] <= 1 and request.POST.get('remove_at_zero') == '1':
            del cart[key]
        else:
            cart[key] = max(cart[key] - 1, 1)
    else:
        return JsonResponse({'success': False, 'error': 'Bad action'}, status=400)

    request.session['cart'] = cart
    request.session.modified = True

    cart_items, subtotal, coupon, discount_amount, total = _get_cart_totals(request)
    line = next((i for i in cart_items if i['line'] == key), None)

    return JsonResponse(_cart_payload(
        request,
        quantity=cart.get(key, 0),
        line_subtotal=_money(line['subtotal']) if line else '0.00',
    ))


'''

NEW_REMOVE = '''@require_POST
def remove_from_cart(request, item_id):
    cart = request.session.get('cart', {})
    # 'line' picks one cart line when the same dish was added with different options
    key = request.POST.get('line') or str(item_id)

    if key in cart:
        del cart[key]
        request.session['cart'] = cart
        request.session.modified = True
        if not _is_ajax(request):
            messages.success(request, 'Item removed from cart.')

    if _is_ajax(request):
        return JsonResponse(_cart_payload(request))
    return redirect('orders:view_cart')


'''

if "_parse_line" in vs:
    print("orders/views.py already patched")
else:
    edits = [
        ("import line", "from menu.models import FoodItem\nfrom booking.models import Table",
         "from menu.models import FoodItem, Option\nfrom booking.models import Table"),
        ("helpers anchor", "def _add_to_session_cart(cart, item_id, quantity):",
         HELPERS + "def _add_to_session_cart(cart, item_id, quantity):"),
        ("add_to_cart", ADD_OLD, ADD_NEW),
        ("bulk_add_to_cart", BULK_OLD, BULK_NEW),
        ("cart totals", TOTALS_OLD, TOTALS_NEW),
        ("cart_data anchor", "def cart_data(request):", ITEM_OPTIONS_VIEW + "def cart_data(request):"),
        ("payload", "'is_veg': i['is_veg'],",
         "'is_veg': i['is_veg'],\n                'line': i['line'],\n                'options_text': i['options_text'],"),
        ("confirm_order items", CONFIRM_OLD, CONFIRM_NEW),
        ("confirm_order create", CREATE_OLD, CREATE_NEW),
    ]
    problems = []
    for label, old, new in edits:
        n = vs.count(old)
        if n != 1:
            problems.append(label + " (found " + str(n) + " times)")
    upd_re = re.compile(r"@require_POST\ndef update_cart_qty\(.*?(?=@require_POST\ndef apply_coupon)", re.S)
    rem_re = re.compile(r"@require_POST\ndef remove_from_cart\(.*?(?=def _parse_arrival)", re.S)
    if len(upd_re.findall(vs)) != 1:
        problems.append("update_cart_qty function")
    if len(rem_re.findall(vs)) != 1:
        problems.append("remove_from_cart function")

    if problems:
        print("orders/views.py: nothing changed. Could not match: " + "; ".join(problems))
    else:
        for label, old, new in edits:
            vs = vs.replace(old, new, 1)
        vs = upd_re.sub(lambda m: NEW_UPDATE, vs, count=1)
        vs = rem_re.sub(lambda m: NEW_REMOVE, vs, count=1)
        vp.write_text(vs, encoding="utf-8")
        print("orders/views.py updated")

# ---------------- 3) orders/urls.py ----------------
up = pathlib.Path("orders/urls.py")
us = up.read_text(encoding="utf-8")
if "item_options" in us:
    print("orders/urls.py already patched")
elif "urlpatterns = [" not in us or "path(" not in us or not re.search(r"(from \. import [^\n]*views|import views)", us):
    print("orders/urls.py: unexpected layout. Nothing changed there. Please upload this file.")
else:
    us = us.replace(
        "urlpatterns = [",
        "urlpatterns = [\n    path('options/<int:item_id>/', views.item_options, name='item_options'),",
        1,
    )
    up.write_text(us, encoding="utf-8")
    print("orders/urls.py updated")
