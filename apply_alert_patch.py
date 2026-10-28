import pathlib
import shutil

VIEWS = pathlib.Path("store/views.py")
URLS = pathlib.Path("store/urls.py")

NEW_BLOCK = r'''@store_manager_required
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
    for r in rows:
        r.store_match = r.store_item or GroceryItem.objects.filter(
            is_active=True, name__iexact=r.item.name
        ).first()
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
'''

src = VIEWS.read_text()
marker = "@store_manager_required\ndef stock_alert("
if src.count(marker) != 1:
    raise SystemExit("Could not find a single stock_alert view in store/views.py. Nothing changed.")

shutil.copy(VIEWS, str(VIEWS) + ".bak_alert")
head = src[: src.index(marker)].rstrip()
VIEWS.write_text(head + "\n\n\n" + NEW_BLOCK)
print("views.py updated")

t = URLS.read_text()
if "kitchen_report_done" in t:
    print("urls.py already has kitchen_report_done")
else:
    anchor = 'path("alerts/", views.stock_alert, name="stock_alert"),'
    if anchor not in t:
        raise SystemExit("Could not find the alerts url line in store/urls.py. Add it by hand.")
    shutil.copy(URLS, str(URLS) + ".bak_alert")
    t = t.replace(
        anchor,
        anchor + '\n    path("alerts/report/<int:pk>/done/", views.kitchen_report_done, name="kitchen_report_done"),',
    )
    URLS.write_text(t)
    print("urls.py updated")
