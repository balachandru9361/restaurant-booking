import os
import re
import shutil
import sys

MODELS = "dashboard/models.py"
VIEWS = "dashboard/views.py"
URLS = "dashboard/urls.py"
BASE = "templates/dashboard/base_admin.html"
STOCK_TPL = "templates/dashboard/kitchen_stock.html"


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def backup(p):
    b = p + ".bak_stock"
    if os.path.exists(p) and not os.path.exists(b):
        shutil.copy(p, b)


MODEL_IMPORTS = [
    ("from decimal import Decimal", "from decimal import Decimal\n"),
    ("from django.db.models.signals import pre_save, post_save",
     "from django.db.models.signals import pre_save, post_save\n"),
    ("from django.dispatch import receiver", "from django.dispatch import receiver\n"),
    ("from django.utils import timezone", "from django.utils import timezone\n"),
]

MODELS_ADD = r'''

class StockRequest(models.Model):
    """Kitchen reports an item as out of stock; admin restocks it; kitchen is notified."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('restocked', 'Restocked'),
    ]

    item = models.ForeignKey(GroceryItem, on_delete=models.CASCADE, related_name='stock_requests')
    note = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='pending')
    requested_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    restocked_at = models.DateTimeField(null=True, blank=True)
    restocked_message = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-id']

    def __str__(self):
        return f'{self.item.name} ({self.status})'


@receiver(pre_save, sender=GroceryItem)
def remember_old_quantity(sender, instance, **kwargs):
    old = None
    if instance.pk:
        old = GroceryItem.objects.filter(pk=instance.pk).values_list('quantity', flat=True).first()
    instance._old_quantity = old


@receiver(post_save, sender=GroceryItem)
def resolve_stock_requests(sender, instance, created, **kwargs):
    """When the quantity goes up, close the pending out-of-stock requests for this item."""
    old = getattr(instance, '_old_quantity', None)
    if created or old is None:
        return
    new_qty = Decimal(str(instance.quantity))
    if new_qty <= Decimal(str(old)):
        return
    pending = StockRequest.objects.filter(item=instance, status='pending')
    if not pending.exists():
        return
    if getattr(instance, '_by_kitchen', False):
        message = ''  # the chef added the stock himself: close silently, no notification
    else:
        message = f'✅ Admin restocked {instance.name}: now {new_qty.normalize():f} {instance.unit}'
    pending.update(status='restocked', restocked_at=timezone.now(), restocked_message=message[:255])
'''

NEW_VIEWS = r'''def kitchen_stock(request):
    """Kitchen 'Out of Stock' page: report an item that ran out and see restock updates."""
    from .models import StockRequest
    items = GroceryItem.objects.all().order_by('name')
    pending_ids = set(StockRequest.objects.filter(status='pending').values_list('item_id', flat=True))
    recent = (StockRequest.objects.filter(status='restocked')
              .exclude(restocked_message='')
              .select_related('item')
              .order_by('-restocked_at')[:5])
    return render(request, 'dashboard/kitchen_stock.html', {
        'grocery_items': items,
        'pending_ids': pending_ids,
        'recent_restocks': recent,
    })


@kitchen_access_required
@require_POST
def kitchen_request_stock(request, pk):
    """Chef reports an item as out of stock -> admin gets an alert."""
    from .models import StockRequest
    item = get_object_or_404(GroceryItem, pk=pk)
    if StockRequest.objects.filter(item=item, status='pending').exists():
        return JsonResponse({'ok': False, 'error': 'Already reported. Waiting for admin to restock.'}, status=400)
    note = (request.POST.get('note') or '').strip()[:120]
    StockRequest.objects.create(item=item, note=note, requested_by=request.user)
    msg = f'🚫 Kitchen: {item.name} is out of stock ({item.quantity} {item.unit} left). Please restock.'
    if note:
        msg += f' Note: {note}'
    Notification.objects.create(kind='stock_out', message=msg[:255], created_by=request.user)
    return JsonResponse({'ok': True, 'name': item.name})


@kitchen_access_required
def kitchen_notifications_poll(request):
    """Latest 'admin restocked' event for the kitchen toast."""
    from .models import StockRequest
    latest = (StockRequest.objects.filter(status='restocked')
              .exclude(restocked_message='')
              .order_by('-restocked_at')
              .first())
    return JsonResponse({
        'latest_ts': int(latest.restocked_at.timestamp() * 1000) if latest and latest.restocked_at else 0,
        'latest_message': latest.restocked_message if latest else '',
        'pending': StockRequest.objects.filter(status='pending').count(),
    })
'''

KITCHEN_JS = r'''{% if not request.user.is_superuser and request.user.profile.role != 'server' %}
<script>
  // ---- Kitchen: live "admin restocked" notification ----
  (function () {
    const KEY = 'kitchenLastRestockTs';

    function alertToast(title, text) {
      let stack = document.getElementById('kitchenAlertStack');
      if (!stack) {
        stack = document.createElement('div');
        stack.id = 'kitchenAlertStack';
        stack.style.cssText = 'position:fixed;top:18px;right:18px;z-index:10001;display:flex;flex-direction:column;gap:10px';
        document.body.appendChild(stack);
      }
      const t = document.createElement('div');
      t.style.cssText = 'min-width:290px;max-width:360px;padding:14px 16px;border-radius:14px;color:#fff;background:#0f766e;box-shadow:0 12px 30px rgba(0,0,0,.25);font-family:Inter,sans-serif';
      const b = document.createElement('b');
      b.style.cssText = 'display:block;font-size:14px;margin-bottom:2px';
      b.textContent = title;
      const s = document.createElement('small');
      s.style.cssText = 'font-size:12.5px;opacity:.95';
      s.textContent = text;
      t.appendChild(b);
      t.appendChild(s);
      stack.appendChild(t);
      setTimeout(function () { t.remove(); }, 6000);
    }

    function poll() {
      fetch("{% url 'dashboard:kitchen_notifications_poll' %}", { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          const stored = localStorage.getItem(KEY);
          if (stored === null) {
            localStorage.setItem(KEY, data.latest_ts);   // first visit: no toast for old updates
            return;
          }
          if (data.latest_ts > parseInt(stored, 10)) {
            localStorage.setItem(KEY, data.latest_ts);
            alertToast('📦 Stock restocked', data.latest_message);
            if (typeof beep === 'function') beep();
          }
        })
        .catch(function () {});
    }
    poll();
    setInterval(poll, 8000);
  })();
</script>
{% endif %}

'''

TEMPLATE = r'''{% extends "dashboard/base_admin.html" %}
{% block admin_content %}
<style>
  .os-head h1 { font-family: 'Sora', sans-serif; font-size: 26px; color: #0d1b2a; margin-bottom: 4px; }
  .os-head p { font-family: 'Inter', sans-serif; color: #6b7280; font-size: 14px; margin-bottom: 20px; }
  .os-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
  .os-card { background: #fff; border: 1px solid #e2e8f0; border-left: 6px solid #14b8a6; border-radius: 14px; padding: 16px; min-width: 0; box-sizing: border-box; }
  .os-card.low { border-left-color: #f59e0b; background: #fffbeb; }
  .os-card.out { border-left-color: #dc2626; background: #fef2f2; }
  .os-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
  .os-top h3 { font-size: 15px; margin: 0; text-transform: capitalize; color: #0d1b2a; }
  .os-badge { font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 99px; white-space: nowrap; background: #ccfbf1; color: #0f766e; }
  .os-card.low .os-badge { background: #fde68a; color: #92400e; }
  .os-card.out .os-badge { background: #fecaca; color: #991b1b; }
  .os-qty { margin: 10px 0 12px; display: flex; align-items: baseline; gap: 6px; color: #64748b; }
  .os-qty b { font-family: 'Sora', sans-serif; font-size: 30px; line-height: 1; color: #0f172a; }
  .os-note { width: 100%; box-sizing: border-box; border: 1px solid #cbd5e1; border-radius: 10px; padding: 9px 10px; font-size: 13.5px; outline: none; margin-bottom: 8px; }
  .os-note:focus { border-color: #0f766e; }
  .os-btn { width: 100%; box-sizing: border-box; border: 0; border-radius: 10px; padding: 10px; font-weight: 700; font-size: 13.5px; cursor: pointer; background: #dc2626; color: #fff; }
  .os-btn:hover { background: #b91c1c; }
  .os-btn:disabled { opacity: .6; cursor: wait; }
  .os-pending { background: #fef3c7; color: #92400e; border-radius: 10px; padding: 10px 12px; font-size: 13px; font-weight: 700; }
  .os-section { background: #fff; border-radius: 14px; padding: 18px 20px; margin-top: 24px; box-shadow: 0 2px 10px rgba(0,0,0,.06); }
  .os-section h2 { font-family: 'Sora', sans-serif; font-size: 15px; color: #0d1b2a; margin: 0 0 12px; padding-bottom: 10px; border-bottom: 2px solid #0f766e; }
  .os-row { display: flex; justify-content: space-between; gap: 12px; padding: 8px 0; border-top: 1px solid #f1f5f9; font-size: 13.5px; color: #374151; }
  .os-row:first-of-type { border-top: 0; }
  .os-row small { color: #9ca3af; white-space: nowrap; }
  .os-empty { color: #9ca3af; font-size: 13px; }
  #osToasts { position: fixed; top: 18px; right: 18px; z-index: 10002; display: flex; flex-direction: column; gap: 10px; }
  .os-toast { min-width: 280px; max-width: 360px; padding: 14px 16px; border-radius: 14px; color: #fff; background: #0f766e; box-shadow: 0 12px 30px rgba(0,0,0,.25); font-size: 13.5px; font-weight: 600; }
  .os-toast.err { background: #b91c1c; }
</style>

<div class="os-head">
  <h1>🚫 Out of Stock</h1>
  <p>Report an item that has run out. The admin gets an alert, and you are notified when it is restocked.</p>
</div>

<div class="os-grid">
  {% for item in grocery_items %}
  <article class="os-card{% if item.quantity <= 0 %} out{% elif item.is_low %} low{% endif %}"
           data-url="{% url 'dashboard:kitchen_request_stock' item.pk %}" data-name="{{ item.name }}">
    <div class="os-top">
      <h3>{{ item.name }}</h3>
      <span class="os-badge">{% if item.quantity <= 0 %}❌ Finished{% elif item.is_low %}⚠ Low stock{% else %}✅ Enough{% endif %}</span>
    </div>
    <div class="os-qty"><b>{{ item.quantity|floatformat:"-2" }}</b><span>{{ item.unit }} left</span></div>
    {% if item.id in pending_ids %}
      <div class="os-pending">⏳ Reported to admin. Waiting for restock.</div>
    {% else %}
      <input class="os-note" type="text" maxlength="120" placeholder="Note for admin (optional)">
      <button type="button" class="os-btn">🚫 Report out of stock</button>
    {% endif %}
  </article>
  {% empty %}
  <p class="os-empty">No grocery items yet. Admin can add items from Manage Grocery.</p>
  {% endfor %}
</div>

<div class="os-section">
  <h2>✅ Recently restocked by admin</h2>
  {% for r in recent_restocks %}
    <div class="os-row"><span>{{ r.restocked_message }}</span><small>{{ r.restocked_at|timesince }} ago</small></div>
  {% empty %}
    <p class="os-empty">No restock updates yet.</p>
  {% endfor %}
</div>

<div id="osToasts"></div>

<script>
(function () {
  const box = document.getElementById('osToasts');

  function toast(text, isError) {
    const t = document.createElement('div');
    t.className = 'os-toast' + (isError ? ' err' : '');
    t.textContent = text;
    box.appendChild(t);
    setTimeout(function () { t.remove(); }, 4500);
  }

  document.querySelectorAll('.os-card').forEach(function (card) {
    const btn = card.querySelector('.os-btn');
    if (!btn) return;
    btn.addEventListener('click', function () {
      const note = card.querySelector('.os-note');
      btn.disabled = true;
      const fd = new FormData();
      fd.append('note', note ? note.value : '');
      fd.append('csrfmiddlewaretoken', '{{ csrf_token }}');
      fetch(card.dataset.url, { method: 'POST', body: fd, headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) { toast(d.error || 'Could not send the alert', true); btn.disabled = false; return; }
          if (note) note.remove();
          btn.outerHTML = '<div class="os-pending">⏳ Reported to admin. Waiting for restock.</div>';
          toast('Alert sent to admin: ' + card.dataset.name, false);
        })
        .catch(function () { toast('Network problem. Please retry.', true); btn.disabled = false; });
    });
  });

  // Refresh every 10 seconds so restock updates show up, but never while typing or sending
  setInterval(function () {
    const typing = Array.from(document.querySelectorAll('.os-note')).some(function (n) {
      return n === document.activeElement || n.value !== '';
    });
    const sending = Array.from(document.querySelectorAll('.os-btn')).some(function (b) { return b.disabled; });
    if (!typing && !sending) location.reload();
  }, 10000);
})();
</script>
{% endblock %}
'''


def main():
    for p in (MODELS, VIEWS, URLS, BASE):
        if not os.path.exists(p):
            sys.exit("Missing file: " + p + "  (run this from the project root)")

    models_src, views_src, urls_src, base_src = read(MODELS), read(VIEWS), read(URLS), read(BASE)
    notes = []

    if "class StockRequest" not in models_src:
        anchor = "from django.contrib.auth.models import User\n"
        if anchor not in models_src:
            sys.exit("models.py: could not find the User import line. Nothing changed.")
        add = "".join(line for key, line in MODEL_IMPORTS
                      if not re.search(r"^\s*" + re.escape(key) + r"\b", models_src, re.M))
        models_src = models_src.replace(anchor, anchor + add, 1)
        if "('stock_out'" not in models_src:
            models_src = models_src.replace(
                "('stock', 'Kitchen stock update'),",
                "('stock', 'Kitchen stock update'),\n        ('stock_out', 'Out of stock alert'),",
                1,
            )
        models_src = models_src.rstrip("\n") + "\n" + MODELS_ADD
        notes.append("models.py: StockRequest model + restock signal added")
    else:
        notes.append("models.py: already patched, skipped")

    if "def kitchen_request_stock" not in views_src:
        pat = re.compile(
            r"def kitchen_stock\(request\):.*?return render\(request, 'dashboard/kitchen_stock\.html', \{'grocery_items': items\}\)\n",
            re.S,
        )
        if not pat.search(views_src):
            sys.exit("views.py: could not find kitchen_stock(). Nothing changed.")
        views_src = pat.sub(lambda m: NEW_VIEWS, views_src, count=1)

        old = "    item.quantity = old + value if mode == 'add' else value\n    item.save()\n"
        new = ("    item.quantity = old + value if mode == 'add' else value\n"
               "    item._by_kitchen = True  # kitchen changes must not trigger the admin-restock notification\n"
               "    item.save()\n")
        if old in views_src:
            views_src = views_src.replace(old, new, 1)
        else:
            notes.append("WARNING views.py: could not mark kitchen_update_grocery as kitchen-made. "
                         "Chef 'New stock arrived' may show an 'Admin restocked' toast.")
        notes.append("views.py: kitchen_stock replaced, request + poll views added")
    else:
        notes.append("views.py: already patched, skipped")

    if "kitchen_request_stock" not in urls_src:
        pat = re.compile(r"^([ \t]*)path\('kitchen/stock/', views\.kitchen_stock, name='kitchen_stock'\),\n", re.M)
        m = pat.search(urls_src)
        if not m:
            sys.exit("urls.py: could not find the kitchen_stock path. Nothing changed.")
        ind = m.group(1)
        extra = (ind + "path('kitchen/stock/<int:pk>/request/', views.kitchen_request_stock, name='kitchen_request_stock'),\n"
                 + ind + "path('kitchen/notifications/poll/', views.kitchen_notifications_poll, name='kitchen_notifications_poll'),\n")
        urls_src = urls_src[: m.end()] + extra + urls_src[m.end():]
        notes.append("urls.py: 2 paths added")
    else:
        notes.append("urls.py: already patched, skipped")

    if "kitchenLastRestockTs" not in base_src:
        m = re.search(r"\{% if request\.user\.is_superuser %\}\s*<script>\s*// ---- Admin: live", base_src)
        if not m:
            sys.exit("base_admin.html: could not find the admin notification script. Nothing changed.")
        base_src = base_src[: m.start()] + KITCHEN_JS + base_src[m.start():]
        notes.append("base_admin.html: kitchen restock toast added")
    else:
        notes.append("base_admin.html: already patched, skipped")

    for p, s in ((MODELS, models_src), (VIEWS, views_src), (URLS, urls_src), (BASE, base_src)):
        backup(p)
        write(p, s)
    backup(STOCK_TPL)
    write(STOCK_TPL, TEMPLATE)
    notes.append("kitchen_stock.html: written")

    print("Done.")
    for n in notes:
        print(" - " + n)


main()
