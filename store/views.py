from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.db import transaction
from django.db.models import F, Q, Sum, ExpressionWrapper, DecimalField
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import GroceryCategoryForm, GroceryItemForm, StockUpdateForm
from .models import GroceryCategory, GroceryItem, StockMovement
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


# ---------- Dashboard ----------

@store_manager_required
def dashboard(request):
    items = GroceryItem.objects.filter(is_active=True)
    value = ExpressionWrapper(
        F("current_stock") * F("cost_per_unit"),
        output_field=DecimalField(max_digits=16, decimal_places=2),
    )
    ctx = {
        "total_items": items.count(),
        "out_count": items.filter(current_stock__lte=0).count(),
        "low_items": items.filter(current_stock__gt=0, current_stock__lte=F("reorder_level"))[:10],
        "low_count": items.filter(current_stock__gt=0, current_stock__lte=F("reorder_level")).count(),
        "out_items": items.filter(current_stock__lte=0)[:10],
        "stock_value": items.aggregate(v=Sum(value))["v"] or Decimal("0"),
        "recent": StockMovement.objects.select_related("item", "created_by")[:8],
    }
    return render(request, "store/dashboard.html", ctx)


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
    form = GroceryItemForm(request.POST or None)
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
    return render(request, "store/move_list.html", {"cfg": cfg, "rows": qs[:300], "q": q})


def _move_add(request, key):
    cfg = MOVE_PAGES[key]
    initial = {}
    pre = request.GET.get("item", "")
    if pre.isdigit():
        initial["item"] = int(pre)
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
    return render(request, "store/move_form.html", {"cfg": cfg, "form": form})


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
    return render(request, "store/stock_alert.html", {
        "out_items": items.filter(current_stock__lte=0),
        "low_items": items.filter(current_stock__gt=0, current_stock__lte=F("reorder_level")),
    })
