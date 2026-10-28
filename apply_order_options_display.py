import pathlib
import shutil

STYLE = 'style="display:block;font-size:.8em;font-weight:600;color:#d97706;margin-top:2px"'
INLINE = 'style="font-size:.8em;font-weight:600;color:#d97706"'
BLOCK = '{% if item.options_text %}<small ' + STYLE + '>{{ item.options_text }}</small>{% endif %}'

# (file, old text, new text, expected matches)
jobs = [
    ("templates/dashboard/kitchen.html",
     "<li><span>{{ item.food_item.name }} x{{ item.quantity }}</span></li>",
     "<li><span>{{ item.food_item.name }} x{{ item.quantity }}</span>" + BLOCK + "</li>",
     3),
    ("templates/dashboard/server.html",
     "<li><span>{{ item.food_item.name }} x{{ item.quantity }}</span></li>",
     "<li><span>{{ item.food_item.name }} x{{ item.quantity }}</span>" + BLOCK + "</li>",
     2),
    ("templates/dashboard/manage_orders.html",
     '<span class="n">{{ item.food_item.name }}</span>',
     '<span class="n">{{ item.food_item.name }}{% if item.options_text %} <small ' + INLINE + '>({{ item.options_text }})</small>{% endif %}</span>',
     1),
    ("templates/orders/order_history.html",
     '<span class="item-name">{{ item.food_item.name }} <span class="item-qty">x {{ item.quantity }}</span></span>',
     '<span class="item-name">{{ item.food_item.name }} <span class="item-qty">x {{ item.quantity }}</span>' + BLOCK + '</span>',
     1),
    ("templates/orders/razorpay_checkout.html",
     '<span class="item-name">{{ item.food_item.name }}<span class="qty">x {{ item.quantity }}</span></span>',
     '<span class="item-name">{{ item.food_item.name }}<span class="qty">x {{ item.quantity }}</span>' + BLOCK + '</span>',
     1),
]

# check everything first, change nothing if any file does not match
problems = []
texts = {}
for path, old, new, count in jobs:
    p = pathlib.Path(path)
    if not p.exists():
        problems.append(path + " (file not found)")
        continue
    s = p.read_text(encoding="utf-8")
    texts[path] = s
    if "item.options_text" in s:
        print(path + ": already patched, skipping")
        continue
    n = s.count(old)
    if n != count:
        problems.append(path + " (found " + str(n) + ", expected " + str(count) + ")")

if problems:
    print("Nothing changed. Could not match: " + "; ".join(problems))
    raise SystemExit

for path, old, new, count in jobs:
    s = texts[path]
    if "item.options_text" in s:
        continue
    p = pathlib.Path(path)
    shutil.copy(p, path + ".bak_optshow")
    p.write_text(s.replace(old, new), encoding="utf-8")
    print(path + ": updated")
