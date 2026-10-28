from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.db import transaction
from django.db.models import Count, F, Q, Sum, ExpressionWrapper, DecimalField
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from menu.models import FoodItem

from .forms import GroceryCategoryForm, GroceryItemForm, StockUpdateForm
from .models import GroceryCategory, GroceryItem, RecipeIngredient, StockMovement
from .permissions import is_store_manager, store_manager_required


# ---------- Login / logout (separate from the customer login) ----------

def store_login(request):
    if is_store_manager(request.user):
        return redirect("store:dashboard")

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        if not is_store_manager(user):
            form.add_error(None, "This account does not have Store Management access.")
        else:
            login(request, user)
            nxt = request.GET.get("next", "")
            if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
                return redirect(nxt)
            return redirect("store:dashboard")
    return render(request, "store/login.html", {"form": form})


@require_POST
def store_logout(request):
    logout(request)
    return redirect("store:login")


# ---------- Dashboard (upgraded: charts, alerts, reorder list, live refresh) ----------

def build_dashboard_data():
    now = timezone.localtime()
    items = GroceryItem.objects.filter(is_active=True)
    out_qs = items.filter(current_stock__lte=0)
    low_qs = items.filter(current_stock__gt=0, current_stock__lte=F("reorder_level"))

    value = items.aggregate(
        v=Sum(ExpressionWrapper(
            F("current_stock") * F("cost_per_unit"),
            output_field=DecimalField(max_digits=16, decimal_places=2),
        ))
    )["v"] or Decimal("0")

    # last 7 days: stock in vs stock out
    start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (StockMovement.objects.filter(created_at__gte=start)
            .annotate(d=TruncDate("created_at")).values("d")
            .annotate(i=Sum("change", filter=Q(change__gt=0)),
                      o=Sum("change", filter=Q(change__lt=0))))
    by_day = {r["d"]: r for r in rows}
    week = []
    for n in range(7):
        day = (start + timedelta(days=n)).date()
        r = by_day.get(day, {})
        week.append({"d": day.strftime("%a"),
                     "in": float(r.get("i") or 0),
                     "out": abs(float(r.get("o") or 0))})

    # most used items by the kitchen (last 7 days)
    top = (StockMovement.objects
           .filter(created_at__gte=start, movement_type=StockMovement.OUT, change__lt=0)
           .values("item__name", "item__unit").annotate(q=Sum("change")).order_by("q")[:5])

    # damage + expiry loss in rupees (last 30 days)
    loss = Decimal("0")
    for m in (StockMovement.objects
              .filter(created_at__gte=now - timedelta(days=30),
                      movement_type__in=[StockMovement.DAMAGE, StockMovement.EXPIRY])
              .select_related("item")):
        loss += abs(m.change) * m.item.cost_per_unit

    recent = StockMovement.objects.select_related("item", "created_by")[:25]

    return {
        "updated": now.strftime("%I:%M:%S %p"),
        "kpi": {"total": items.count(), "low": low_qs.count(), "out": out_qs.count(),
                "value": float(value), "loss30": float(loss)},
        "week": week,
        "low": [{"name": i.name, "stock": float(i.current_stock), "unit": i.unit,
                 "level": float(i.reorder_level), "supplier": i.supplier}
                for i in low_qs.order_by("current_stock")],
        "out": [{"name": i.name, "unit": i.unit, "level": float(i.reorder_level),
                 "supplier": i.supplier} for i in out_qs],
        "top": [{"name": t["item__name"], "unit": t["item__unit"], "qty": abs(float(t["q"]))}
                for t in top],
        "recent": [{"when": timezone.localtime(m.created_at).strftime("%d %b, %I:%M %p"),
                    "item": m.item.name, "unit": m.item.unit, "change": float(m.change),
                    "after": float(m.stock_after), "reason": m.movement_type.lower(),
                    "by": (m.created_by.get_username() if m.created_by else "")}
                   for m in recent],
    }


@store_manager_required
def dashboard(request):
    return render(request, "store/dashboard.html", {"initial": build_dashboard_data()})


@store_manager_required
def dashboard_api(request):
    return JsonResponse(build_dashboard_data())


# ---------- Grocery items ----------

@store_manager_required
def item_list(request):
    qs = GroceryItem.objects.filter(is_active=True).select_related("category")
    q = request.GET.get("q", "").strip()
    flt = request.GET.get("filter", "")
    cat = request.GET.get("category", "")

    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(supplier__icontains=q))
    if cat.isdigit():
        qs = qs.filter(category_id=int(cat))
    if flt == "low":
        qs = qs.filter(current_stock__gt=0, current_stock__lte=F("reorder_level"))
    elif flt == "out":
        qs = qs.filter(current_stock__lte=0)

    return render(request, "store/item_list.html", {
        "items": qs,
        "q": q,
        "flt": flt,
        "cat": cat,
        "categories": GroceryCategory.objects.all(),
    })


@store_manager_required
def item_add(request):
    # ?name=...&unit=... lets other pages open this form with the item name filled in
    initial = {}
    if request.GET.get("name"):
        initial["name"] = request.GET["name"].strip()[:200]
    if request.GET.get("unit"):
        initial["unit"] = request.GET["unit"].strip()
    form = GroceryItemForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            item = form.save(commit=False)
            opening = form.cleaned_data.get("opening_stock") or Decimal("0")
            item.current_stock = opening
            item.save()
            if opening > 0:
                StockMovement.objects.create(
                    item=item, movement_type=StockMovement.IN, change=opening,
                    stock_after=opening, note="Opening stock", created_by=request.user,
                )
        messages.success(request, f"'{item.name}' added.")
        if request.GET.get("then") == "stock_in":
            # came from Stock Alert: go straight to Stock In with this item selected
            return redirect(f"{reverse('store:stock_in_add')}?item={item.pk}")
        return redirect("store:item_list")
    return render(request, "store/item_form.html", {"form": form, "title": "Add Grocery Item"})


@store_manager_required
def item_edit(request, pk):
    item = get_object_or_404(GroceryItem, pk=pk, is_active=True)
    form = GroceryItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"'{item.name}' updated.")
        return redirect("store:item_list")
    return render(request, "store/item_form.html", {"form": form, "title": f"Edit {item.name}"})


@store_manager_required
@require_POST
def item_remove(request, pk):
    item = get_object_or_404(GroceryItem, pk=pk, is_active=True)
    item.is_active = False  # soft delete, so stock history is kept
    item.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"'{item.name}' removed from the list.")
    return redirect("store:item_list")


# ---------- Stock update ----------

@store_manager_required
def stock_update(request, pk):
    item = get_object_or_404(GroceryItem, pk=pk, is_active=True)
    form = StockUpdateForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        kind = form.cleaned_data["movement_type"]
        qty = form.cleaned_data["quantity"]
        note = form.cleaned_data["note"]

        with transaction.atomic():
            locked = GroceryItem.objects.select_for_update().get(pk=item.pk)
            old = locked.current_stock

            if kind == StockMovement.IN:
                new = old + qty
            elif kind == StockMovement.OUT:
                new = old - qty
            else:
                new = qty

            if new < 0:
                form.add_error("quantity", f"Only {old.normalize()} {locked.unit} in stock.")
            else:
                locked.current_stock = new
                locked.save(update_fields=["current_stock", "updated_at"])
                StockMovement.objects.create(
                    item=locked, movement_type=kind, change=new - old,
                    stock_after=new, note=note, created_by=request.user,
                )
                messages.success(request, f"{locked.name} stock is now {new.normalize()} {locked.unit}.")
                return redirect("store:item_list")

    return render(request, "store/stock_update.html", {"item": item, "form": form})


# ---------- Categories ----------

@store_manager_required
def categories(request):
    form = GroceryCategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Category added.")
        return redirect("store:categories")
    return render(request, "store/categories.html", {
        "form": form,
        "categories": GroceryCategory.objects.all(),
    })


@store_manager_required
@require_POST
def category_delete(request, pk):
    cat = get_object_or_404(GroceryCategory, pk=pk)
    cat.delete()  # items keep existing, just become uncategorised
    messages.success(request, "Category deleted.")
    return redirect("store:categories")


# ---------- Stock history ----------

@store_manager_required
def movements(request):
    qs = StockMovement.objects.select_related("item", "created_by")
    item_id = request.GET.get("item", "")
    if item_id.isdigit():
        qs = qs.filter(item_id=int(item_id))
    return render(request, "store/movements.html", {
        "movements": qs[:200],
        "items": GroceryItem.objects.all(),
        "selected": item_id,
    })


# ---------- Stock In / Stock Out / Damage-Expired / Stock Alert ----------

from .forms import StockMoveForm

MOVE_PAGES = {
    "in": {
        "title": "Stock In", "types": [StockMovement.IN], "sign": 1, "reasons": None,
        "list_url": "store:stock_in", "add_url": "store:stock_in_add",
        "hint": "Stock received from suppliers.",
    },
    "out": {
        "title": "Stock Out", "types": [StockMovement.OUT], "sign": -1, "reasons": None,
        "list_url": "store:stock_out", "add_url": "store:stock_out_add",
        "hint": "Stock given to the kitchen or used up.",
    },
    "damage": {
        "title": "Damage / Expired", "types": [StockMovement.DAMAGE, StockMovement.EXPIRY], "sign": -1,
        "reasons": [(StockMovement.DAMAGE, "Damaged"), (StockMovement.EXPIRY, "Expired")],
        "list_url": "store:damage", "add_url": "store:damage_add",
        "hint": "Stock thrown away because it was damaged or expired.",
    },
}


def _move_list(request, key):
    cfg = MOVE_PAGES[key]
    qs = StockMovement.objects.filter(movement_type__in=cfg["types"]).select_related("item", "created_by")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(item__name__icontains=q)
    rows = list(qs[:300])
    for r in rows:
        r.value = abs(r.change) * r.item.cost_per_unit  # rupee value of each entry
    return render(request, "store/move_list.html", {"cfg": cfg, "rows": rows, "q": q})


def _move_add(request, key):
    cfg = MOVE_PAGES[key]
    initial = {}
    pre = request.GET.get("item", "")
    if pre.isdigit():
        initial["item"] = int(pre)
    else:
        # Item had no exact store match: try to pick one from the name (?q=Goat intestine)
        name = request.GET.get("q", "").strip()
        if name:
            qs = GroceryItem.objects.filter(is_active=True)
            match = qs.filter(name__iexact=name).first() or qs.filter(name__icontains=name).first()
            if match:
                initial["item"] = match.pk
    form = StockMoveForm(request.POST or None, reasons=cfg["reasons"], initial=initial)
    if request.method == "POST" and form.is_valid():
        qty = form.cleaned_data["quantity"]
        kind = form.cleaned_data.get("reason") or cfg["types"][0]
        note = form.cleaned_data["note"]
        with transaction.atomic():
            locked = GroceryItem.objects.select_for_update().get(pk=form.cleaned_data["item"].pk)
            old = locked.current_stock
            new = old + qty * cfg["sign"]
            if new < 0:
                form.add_error("quantity", f"Only {old.normalize()} {locked.unit} in stock.")
            else:
                locked.current_stock = new
                locked.save(update_fields=["current_stock", "updated_at"])
                StockMovement.objects.create(
                    item=locked, movement_type=kind, change=new - old,
                    stock_after=new, note=note, created_by=request.user,
                )
                messages.success(request, f"{locked.name} stock is now {new.normalize()} {locked.unit}.")
                return redirect(cfg["list_url"])
    return render(request, "store/move_form.html", {
        "cfg": cfg,
        "form": form,
        "prefill_q": request.GET.get("q", "").strip(),
    })


@store_manager_required
def stock_in(request):
    return _move_list(request, "in")


@store_manager_required
def stock_in_add(request):
    return _move_add(request, "in")


@store_manager_required
def stock_out(request):
    return _move_list(request, "out")


@store_manager_required
def stock_out_add(request):
    return _move_add(request, "out")


@store_manager_required
def damage(request):
    return _move_list(request, "damage")


@store_manager_required
def damage_add(request):
    return _move_add(request, "damage")


@store_manager_required
def stock_alert(request):
    items = GroceryItem.objects.filter(is_active=True).select_related("category")
    out_items = list(items.filter(current_stock__lte=0))
    low_items = list(items.filter(current_stock__gt=0, current_stock__lte=F("reorder_level")))

    total_cost = Decimal("0")
    for i in out_items + low_items:
        i.needed = max(i.reorder_level - i.current_stock, Decimal("0"))
        i.cost = i.needed * i.cost_per_unit
        total_cost += i.cost

    return render(request, "store/stock_alert.html", {
        "out_items": out_items,
        "low_items": low_items,
        "kitchen_reports": _pending_kitchen_reports(),
        "total_cost": total_cost,
    })


def _pending_kitchen_reports():
    # Out-of-stock reports sent by the kitchen that the store has not answered yet.
    from dashboard.models import StockRequest

    rows = list(
        StockRequest.objects.filter(status="pending")
        .select_related("item", "store_item", "requested_by")
    )
    active = GroceryItem.objects.filter(is_active=True)
    for r in rows:
        match = r.store_item
        if not match:
            # exact name first, then a partial name match
            match = (
                active.filter(name__iexact=r.item.name).first()
                or active.filter(name__icontains=r.item.name).first()
            )
        r.store_match = match
    return rows


@store_manager_required
@require_POST
def kitchen_report_done(request, pk):
    from dashboard.models import Notification, StockRequest

    req = get_object_or_404(StockRequest, pk=pk, status="pending")
    msg = f"Store restocked {req.item.name}"
    req.status = "restocked"
    req.restocked_at = timezone.now()
    req.restocked_message = msg[:255]
    req.save(update_fields=["status", "restocked_at", "restocked_message"])
    Notification.objects.create(
        kind="stock_in", message=msg[:255], created_by=request.user, is_read=True
    )
    messages.success(request, f"{req.item.name} marked as restocked. Kitchen is notified.")
    return redirect("store:stock_alert")


# ---------- Recipes (which grocery items each dish uses) ----------

@store_manager_required
def recipe_list(request):
    """All dishes with how many recipe lines each one has."""
    all_foods = FoodItem.objects.all()
    foods = (
        all_foods.select_related("category")
        .annotate(line_count=Count("recipe_items"))
        .order_by("category__name", "name")
    )

    q = request.GET.get("q", "").strip()
    flt = request.GET.get("filter", "")
    if q:
        foods = foods.filter(Q(name__icontains=q) | Q(category__name__icontains=q))
    if flt == "none":
        foods = foods.filter(line_count=0)
    elif flt == "done":
        foods = foods.filter(line_count__gt=0)

    total = all_foods.count()
    with_recipe = all_foods.filter(recipe_items__isnull=False).distinct().count()

    return render(request, "store/recipe_list.html", {
        "foods": foods,
        "q": q,
        "flt": flt,
        "total": total,
        "with_recipe": with_recipe,
        "without_recipe": total - with_recipe,
    })


@store_manager_required
def recipe_edit(request, pk):
    """Show the recipe of one dish and add or update an ingredient line."""
    food = get_object_or_404(FoodItem.objects.select_related("category"), pk=pk)
    ingredients = GroceryItem.objects.filter(is_active=True).select_related("category").order_by("name")

    if request.method == "POST":
        ing_id = request.POST.get("ingredient", "")
        raw_qty = request.POST.get("quantity", "").strip()

        ingredient = ingredients.filter(pk=int(ing_id)).first() if ing_id.isdigit() else None
        try:
            qty = Decimal(raw_qty).quantize(Decimal("0.001"))
        except (InvalidOperation, ValueError):
            qty = None

        if ingredient is None:
            messages.error(request, "Please choose an ingredient.")
        elif qty is None or not qty.is_finite() or qty <= 0 or qty >= Decimal("10000000"):
            messages.error(request, "Enter a quantity greater than 0 (in the ingredient's unit).")
        else:
            RecipeIngredient.objects.update_or_create(
                food_item=food, ingredient=ingredient, defaults={"quantity": qty},
            )
            messages.success(
                request, f"{ingredient.name}: {qty.normalize()} {ingredient.unit} saved for {food.name}."
            )
            return redirect("store:recipe_edit", pk=food.pk)

    lines = food.recipe_items.select_related("ingredient").order_by("ingredient__name")
    return render(request, "store/recipe_edit.html", {
        "food": food,
        "lines": lines,
        "ingredients": ingredients,
    })


@store_manager_required
@require_POST
def recipe_line_delete(request, pk):
    line = get_object_or_404(RecipeIngredient.objects.select_related("food_item", "ingredient"), pk=pk)
    food_id = line.food_item_id
    name = line.ingredient.name
    line.delete()
    messages.success(request, f"{name} removed from the recipe.")
    return redirect("store:recipe_edit", pk=food_id)