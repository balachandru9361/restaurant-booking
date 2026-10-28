import pathlib
import shutil

p = pathlib.Path("templates/orders/cart.html")
s = p.read_text(encoding="utf-8")

if "ci-opts" in s:
    print("cart.html already patched")
    raise SystemExit

# (label, old, new, expected number of matches)
edits = [
    ("options text under the name",
     '<p class="ci-each">₹{{ item.price|inr }} each</p>',
     '<p class="ci-each">₹{{ item.price|inr }} each</p>{% if item.options_text %}<p class="ci-opts">{{ item.options_text }}</p>{% endif %}',
     1),
    ("+/- buttons carry the cart line",
     "data-url=\"{% url 'orders:update_cart_qty' item.id %}\"",
     "data-url=\"{% url 'orders:update_cart_qty' item.id %}\" data-line=\"{{ item.line }}\"",
     2),
    ("+/- request sends the line",
     "body: new URLSearchParams({ action: btn.dataset.action })",
     "body: new URLSearchParams({ action: btn.dataset.action, line: btn.dataset.line || '' })",
     1),
    ("remove form sends the line",
     "action=\"{% url 'orders:remove_from_cart' item.id %}\">{% csrf_token %}",
     "action=\"{% url 'orders:remove_from_cart' item.id %}\">{% csrf_token %}<input type=\"hidden\" name=\"line\" value=\"{{ item.line }}\">",
     1),
    ("style block",
     '<div class="cart-page">',
     '<style>\n.ci-opts { margin:3px 0 0; font-size:.8rem; color:#6b7280; line-height:1.35; word-break:break-word; }\n</style>\n\n<div class="cart-page">',
     1),
]

problems = []
for label, old, new, count in edits:
    n = s.count(old)
    if n != count:
        problems.append(label + " (found " + str(n) + ", expected " + str(count) + ")")

if problems:
    print("Nothing changed. Could not match: " + "; ".join(problems))
    raise SystemExit

shutil.copy(p, str(p) + ".bak_options2")
for label, old, new, count in edits:
    s = s.replace(old, new)
p.write_text(s, encoding="utf-8")
print("cart.html updated")
