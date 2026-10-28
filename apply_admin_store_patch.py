import pathlib
import shutil

STAFF_FN = '''def is_store_staff(user):
    # True only for members of the Store Manager group (not admin-only superusers).
    return bool(
        user.is_authenticated
        and user.is_active
        and user.groups.filter(name=STORE_GROUP).exists()
    )

'''

CHECK = '''        # Admin (superuser outside the Store Manager group) can only look, not change.
        if request.method not in ("GET", "HEAD", "OPTIONS") and not is_store_staff(request.user):
            messages.error(request, "Admin has view-only access to Store pages.")
            return redirect("store:dashboard")
'''

ADMIN_LINKS = """        <a href="{% url 'store:dashboard' %}">🛒 Store Dashboard</a>
        <a href="{% url 'store:stock_in' %}">📥 Stock In</a>
        <a href="{% url 'store:stock_out' %}">📤 Stock Out</a>
        <a href="{% url 'store:damage' %}">🗑️ Damage / Expired</a>
        <a href="{% url 'store:movements' %}">📜 Stock History</a>
        <a href="{% url 'store:item_list' %}">🥬 Groceries</a>
        <a href="{% url 'store:stock_alert' %}">⚠️ Stock Alert</a>
"""

BACK_LINK = """    {% if request.user.is_superuser %}<a href="{% url 'dashboard:admin_home' %}">⬅️ Back to Admin</a>{% endif %}
"""

# ---- 1) permissions.py ----
p = pathlib.Path("store/permissions.py")
s = p.read_text()
if "is_store_staff" in s:
    print("permissions.py already patched")
else:
    shutil.copy(p, str(p) + ".bak_admin")
    s = s.replace("from django.shortcuts import redirect",
                  "from django.contrib import messages\nfrom django.shortcuts import redirect", 1)
    s = s.replace("def store_manager_required(view):", STAFF_FN + "\ndef store_manager_required(view):", 1)
    s = s.replace("        return view(request, *args, **kwargs)",
                  CHECK + "        return view(request, *args, **kwargs)", 1)
    p.write_text(s)
    print("permissions.py updated")

# ---- 2) admin sidebar ----
a = pathlib.Path("templates/dashboard/base_admin.html")
lines = a.read_text().splitlines(keepends=True)
if any("store:stock_alert" in l for l in lines):
    print("base_admin.html already has Store links")
else:
    idx = next((i for i, l in enumerate(lines) if "dashboard:sales_reports" in l), None)
    if idx is None:
        print("Could not find the Sales / Reports link in base_admin.html. Nothing changed there.")
    else:
        shutil.copy(a, str(a) + ".bak_storelinks")
        lines.insert(idx + 1, ADMIN_LINKS)
        a.write_text("".join(lines))
        print("base_admin.html updated")

# ---- 3) store sidebar: back link for admin ----
b = pathlib.Path("templates/store/store_base.html")
bl = b.read_text().splitlines(keepends=True)
if any("Back to Admin" in l for l in bl):
    print("store_base.html already has Back to Admin")
else:
    idx = next((i for i, l in enumerate(bl) if "Store Management</h2>" in l), None)
    if idx is None:
        print("Could not find the Store Management heading in store_base.html. Nothing changed there.")
    else:
        shutil.copy(b, str(b) + ".bak_backlink")
        bl.insert(idx + 1, BACK_LINK)
        b.write_text("".join(bl))
        print("store_base.html updated")
