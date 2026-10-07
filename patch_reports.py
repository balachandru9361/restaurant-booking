import re
import shutil
import sys

PATH = "dashboard/views.py"

NEW_FUNC = '''def sales_reports(request):
    ranges = [
        ('today', 'Today'),
        ('week', 'This Week'),
        ('month', 'This Month'),
        ('year', 'This Year'),
        ('all', 'All Time'),
    ]
    labels = dict(ranges)

    rng = request.GET.get('range', 'today')
    if rng not in labels:
        rng = 'today'

    today = timezone.localdate()
    if rng == 'today':
        start = today
    elif rng == 'week':
        start = today - timedelta(days=today.weekday())  # Monday
    elif rng == 'month':
        start = today.replace(day=1)
    elif rng == 'year':
        start = today.replace(month=1, day=1)
    else:
        start = None

    orders = Order.objects.filter(status__in=PAID_STATUSES)
    if start:
        orders = orders.filter(created_at__date__gte=start)

    # CSV export: all orders in the selected range
    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="sales_%s.csv"' % rng
        writer = csv.writer(response)
        writer.writerow(['Order ID', 'Customer', 'Amount', 'Payment', 'Payment Status', 'Status', 'Date'])
        for o in orders.select_related('user').order_by('-created_at'):
            writer.writerow([
                o.id,
                o.user.get_full_name() or o.user.username,
                o.total_amount,
                o.payment_method,
                o.payment_status,
                o.status,
                timezone.localtime(o.created_at).strftime('%d %b %Y %H:%M'),
            ])
        return response

    def total(qs):
        return qs.aggregate(Sum('total_amount'))['total_amount__sum'] or 0

    total_orders = orders.count()
    total_revenue = total(orders)
    avg_order_value = round(total_revenue / total_orders) if total_orders > 0 else 0

    online_orders = orders.filter(payment_method='online')
    cash_orders = orders.filter(payment_method='cash')
    cash_collected_orders = cash_orders.exclude(payment_status='cash_pending')
    cash_pending_orders = cash_orders.filter(payment_status='cash_pending')

    context = {
        'range': rng,
        'range_label': labels[rng],
        'ranges': ranges,
        'total_revenue': total_revenue,
        'total_orders': total_orders,
        'avg_order_value': avg_order_value,
        'recent_orders': orders.select_related('user').order_by('-created_at')[:50],
        'online_revenue': total(online_orders),
        'online_count': online_orders.count(),
        'cash_revenue': total(cash_collected_orders),
        'cash_count': cash_collected_orders.count(),
        'cash_pending_amount': total(cash_pending_orders),
        'cash_pending_count': cash_pending_orders.count(),
    }
    return render(request, 'dashboard/sales_reports.html', context)
'''

IMPORTS = [
    ("import csv", "import csv\n"),
    ("from datetime import timedelta", "from datetime import timedelta\n"),
    ("from django.http import HttpResponse", "from django.http import HttpResponse\n"),
    ("from django.utils import timezone", "from django.utils import timezone\n"),
]


def main():
    src = open(PATH, encoding="utf-8").read()
    m = re.search(
        r"def sales_reports\(request\):.*?return render\(request, 'dashboard/sales_reports\.html', context\)\n",
        src,
        re.S,
    )
    if not m:
        sys.exit("Could not find sales_reports() - nothing changed.")

    shutil.copy(PATH, PATH + ".bak_reports")
    out = src[: m.start()] + NEW_FUNC + src[m.end():]

    missing = [line for key, line in IMPORTS if not re.search(r"^\s*" + re.escape(key) + r"\b", out, re.M)]
    if missing:
        out = "".join(missing) + out

    open(PATH, "w", encoding="utf-8").write(out)
    print("Done. sales_reports() replaced. Backup: " + PATH + ".bak_reports")
    if missing:
        print("Added imports: " + ", ".join(l.strip() for l in missing))


main()
