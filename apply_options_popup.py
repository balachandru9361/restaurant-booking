import pathlib
import re
import shutil

p = pathlib.Path("templates/menu/menu_list.html")
s = p.read_text(encoding="utf-8")

if 'id="itemModal"' not in s:
    print("The simple item popup is not in menu_list.html yet. Run apply_item_modal_patch.py first.")
    raise SystemExit
if "imOptions" in s:
    print("menu_list.html already has the option popup")
    raise SystemExit
if "item_options" not in pathlib.Path("orders/urls.py").read_text(encoding="utf-8"):
    print("Step 1 (backend) is not applied yet. Run apply_options_backend.py first.")
    raise SystemExit

NEW_CSS = r"""<style id="itemModalStyle2">
    /* ===== POPUP OPTION GROUPS ===== */
    .im-group { padding: 12px 0; border-top: 1px dashed #e3dccb; }
    .im-gtitle { font-weight: 800; color: #1b2233; font-size: 0.95rem; margin-bottom: 10px; }
    .im-gtitle small { color: #8a93a6; font-weight: 600; margin-left: 6px; }
    .im-olist { display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 10px; }
    .im-opt {
        position: relative; display: flex; flex-direction: column; align-items: center;
        justify-content: center; gap: 4px; min-height: 64px; padding: 10px 8px;
        border: 2px solid #e3dccb; border-radius: 14px; background: #fff; color: #1b2233;
        font-family: inherit; font-size: 0.82rem; font-weight: 700; text-align: center; cursor: pointer;
        transition: border-color 0.15s ease, background 0.15s ease;
    }
    .im-opt img { width: 54px; height: 54px; object-fit: cover; border-radius: 10px; }
    .im-opt .im-oprice { font-size: 0.74rem; color: #6b7280; font-weight: 600; }
    .im-opt.sel { border-color: #d9a441; background: #fff8e6; }
    .im-opt.sel::after {
        content: "\2713"; position: absolute; top: 5px; right: 7px;
        color: #b45309; font-size: 0.85rem; font-weight: 800;
    }
    .im-loading { color: #8a93a6; font-size: 0.88rem; padding: 10px 0; margin: 0; }
    .cp-opts { font-size: 0.68rem; color: #6b7280; line-height: 1.3; margin-bottom: 4px; word-break: break-word; }
</style>

"""

NEW_JS = r"""<script>
    // Item popup: ADD opens it, shows the dish options (side, add-ons ...), then adds to the cart.
    (function () {
        var overlay = document.getElementById('itemModal');
        if (!overlay) { return; }

        var MAX_QTY = 20;
        var OPTIONS_URL = "{% url 'orders:item_options' 0 %}";
        var RUPEE = '\u20B9';
        var el = {
            img: document.getElementById('imImg'),
            name: document.getElementById('imName'),
            dot: document.getElementById('imDot'),
            desc: document.getElementById('imDesc'),
            qty: document.getElementById('imQty'),
            total: document.getElementById('imTotal'),
            minus: document.getElementById('imMinus'),
            plus: document.getElementById('imPlus'),
            add: document.getElementById('imAdd'),
            close: document.getElementById('imClose'),
            options: document.getElementById('imOptions')
        };
        var current = null;
        var qty = 1;
        var groups = [];
        var chosen = {};      // group id -> array of chosen option ids
        var loading = false;
        var busy = false;

        function extrasTotal() {
            var sum = 0;
            groups.forEach(function (g) {
                (chosen[g.id] || []).forEach(function (oid) {
                    g.options.forEach(function (o) {
                        if (o.id === oid) { sum += parseFloat(o.extra_price) || 0; }
                    });
                });
            });
            return sum;
        }

        function missingRequired() {
            for (var i = 0; i < groups.length; i++) {
                if (groups[i].required && !(chosen[groups[i].id] || []).length) { return groups[i]; }
            }
            return null;
        }

        function allChosenIds() {
            var ids = [];
            groups.forEach(function (g) {
                (chosen[g.id] || []).forEach(function (oid) { ids.push(oid); });
            });
            return ids;
        }

        function refresh() {
            el.qty.textContent = qty;
            el.total.textContent = formatMoney((current.price + extrasTotal()) * qty);
            el.minus.disabled = qty <= 1;
            el.plus.disabled = qty >= MAX_QTY;
            el.add.disabled = loading || busy || !!missingRequired();
        }

        function paintSelected() {
            el.options.querySelectorAll('.im-opt').forEach(function (b) {
                var gid = parseInt(b.dataset.group, 10);
                var oid = parseInt(b.dataset.option, 10);
                var on = (chosen[gid] || []).indexOf(oid) > -1;
                b.classList.toggle('sel', on);
                b.setAttribute('aria-pressed', on ? 'true' : 'false');
            });
        }

        function renderOptions() {
            el.options.innerHTML = '';
            groups.forEach(function (g) {
                var wrap = document.createElement('div');
                wrap.className = 'im-group';

                var title = document.createElement('div');
                title.className = 'im-gtitle';
                title.textContent = g.title;
                var hint = document.createElement('small');
                if (g.required) { hint.textContent = 'Required'; }
                else if (g.type === 'multi') { hint.textContent = g.max_select ? 'Choose up to ' + g.max_select : 'Choose any'; }
                else { hint.textContent = 'Optional'; }
                title.appendChild(hint);
                wrap.appendChild(title);

                var list = document.createElement('div');
                list.className = 'im-olist';
                g.options.forEach(function (o) {
                    var b = document.createElement('button');
                    b.type = 'button';
                    b.className = 'im-opt';
                    b.dataset.group = g.id;
                    b.dataset.option = o.id;
                    if (o.image_url) {
                        var im = document.createElement('img');
                        im.src = o.image_url;
                        im.alt = '';
                        b.appendChild(im);
                    }
                    var nm = document.createElement('span');
                    nm.textContent = o.name;
                    b.appendChild(nm);
                    var extra = parseFloat(o.extra_price) || 0;
                    if (extra > 0) {
                        var pr = document.createElement('span');
                        pr.className = 'im-oprice';
                        pr.textContent = '+ ' + RUPEE + extra.toFixed(2);
                        b.appendChild(pr);
                    }
                    list.appendChild(b);
                });
                wrap.appendChild(list);
                el.options.appendChild(wrap);
            });
            paintSelected();
        }

        el.options.addEventListener('click', function (e) {
            var b = e.target.closest('.im-opt');
            if (!b || !current) { return; }
            var gid = parseInt(b.dataset.group, 10);
            var oid = parseInt(b.dataset.option, 10);
            var g = groups.filter(function (x) { return x.id === gid; })[0];
            if (!g) { return; }
            var list = (chosen[gid] || []).slice();
            var idx = list.indexOf(oid);

            if (g.type === 'single') {
                // tap the chosen one again to clear it (only when the group is optional)
                list = (idx > -1 && !g.required) ? [] : [oid];
            } else if (idx > -1) {
                list.splice(idx, 1);
            } else {
                if (g.max_select && list.length >= g.max_select) {
                    showToast('You can choose up to ' + g.max_select + ' for ' + g.title, true);
                    return;
                }
                list.push(oid);
            }
            chosen[gid] = list;
            paintSelected();
            refresh();
        });

        function message(text) {
            el.options.innerHTML = '';
            var m = document.createElement('p');
            m.className = 'im-loading';
            m.textContent = text;
            el.options.appendChild(m);
        }

        function openModal(form) {
            var card = form.closest('.item-card');
            var box = card.querySelector('.item-select-checkbox');
            var img = card.querySelector('.menu-slide-img');
            var title = card.querySelector('h3');
            var rawPrice = box ? String(box.dataset.price) : '0';
            var isVeg = card.dataset.veg === '1';

            current = { form: form, price: parseFloat(rawPrice.replace(/,/g, '')) || 0 };
            qty = 1;
            groups = [];
            chosen = {};
            busy = false;
            loading = false;

            el.img.src = img ? img.src : NO_IMAGE;
            el.img.alt = title ? title.textContent : '';
            el.name.textContent = box && box.dataset.name ? box.dataset.name : (title ? title.textContent : '');
            el.dot.className = 'dt-ic ' + (isVeg ? 'dt-veg' : 'dt-non');
            el.desc.textContent = form.dataset.desc || 'Freshly prepared and served hot.';
            el.add.textContent = 'ADD TO CART';
            el.options.innerHTML = '';

            overlay.classList.add('open');
            overlay.setAttribute('aria-hidden', 'false');
            document.documentElement.classList.add('im-lock');

            if (form.dataset.hasOptions === '1') {
                loading = true;
                message('Loading options...');
                refresh();
                fetch(urlFor(OPTIONS_URL, form.dataset.itemId), {headers: {'X-Requested-With': 'XMLHttpRequest'}})
                    .then(function (r) { return r.json(); })
                    .then(function (d) {
                        if (!current || current.form !== form) { return; }
                        groups = d.groups || [];
                        groups.forEach(function (g) {
                            var defaults = g.options.filter(function (o) { return o.is_default; })
                                .map(function (o) { return o.id; });
                            chosen[g.id] = g.type === 'single' ? defaults.slice(0, 1) : defaults;
                        });
                        loading = false;
                        renderOptions();
                        refresh();
                    })
                    .catch(function (err) {
                        console.error(err);
                        if (current && current.form === form) {
                            message('Could not load the options. Close and try again.');
                        }
                    });
            } else {
                refresh();
            }
        }

        function closeModal() {
            overlay.classList.remove('open');
            overlay.setAttribute('aria-hidden', 'true');
            document.documentElement.classList.remove('im-lock');
            current = null;
        }

        // Capture phase: runs before the old direct-add handler, so ADD opens the popup instead
        document.addEventListener('submit', function (e) {
            var form = e.target;
            if (!form.classList || !form.classList.contains('add-to-cart-form')) { return; }
            e.preventDefault();
            e.stopPropagation();
            openModal(form);
        }, true);

        el.minus.addEventListener('click', function () { if (qty > 1) { qty--; refresh(); } });
        el.plus.addEventListener('click', function () { if (qty < MAX_QTY) { qty++; refresh(); } });
        el.close.addEventListener('click', closeModal);
        overlay.addEventListener('click', function (e) { if (e.target === overlay) { closeModal(); } });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && overlay.classList.contains('open')) { closeModal(); }
        });

        el.add.addEventListener('click', function () {
            if (!current || loading || missingRequired()) { return; }
            var data = new URLSearchParams();
            data.append('quantity', qty);
            allChosenIds().forEach(function (id) { data.append('options', id); });

            busy = true;
            el.add.disabled = true;
            el.add.textContent = 'Adding...';
            postForm(current.form.action, data)
                .then(function (res) {
                    busy = false;
                    el.add.textContent = 'ADD TO CART';
                    if (res.success) {
                        renderCartPanel(res);
                        showToast(res.message || 'Item added to cart!');
                        closeModal();
                    } else {
                        refresh();
                        showToast(res.error || 'Could not add item.', true);
                    }
                })
                .catch(function (err) {
                    console.error(err);
                    busy = false;
                    el.add.textContent = 'ADD TO CART';
                    refresh();
                    showToast('Failed to add item. Please try again.', true);
                });
        });
    })();
</script>
"""

simple = [
    ("form tag",
     'data-item-id="{{ item.id }}" data-desc=',
     'data-item-id="{{ item.id }}" data-has-options="{% if item.option_groups.exists %}1{% else %}0{% endif %}" data-desc='),
    ("popup html", '<div class="im-qtyrow">', '<div id="imOptions"></div>\n            <div class="im-qtyrow">'),
    ("popup css anchor", '<style id="itemModalStyle">', NEW_CSS + '<style id="itemModalStyle">'),
    ("card stepper sync",
     "const entry = map[form.dataset.itemId];",
     "const entry = form.dataset.hasOptions === '1' ? null : map[form.dataset.itemId];"),
    ("panel item tag",
     "'<div class=\"cp-item\" data-id=\"' + i.id + '\">' +",
     "'<div class=\"cp-item\" data-id=\"' + i.id + '\" data-line=\"' + escapeHtml(i.line || '') + '\">' +"),
    ("panel price line",
     "'<div class=\"cp-price\">' + formatMoney(i.price) + '</div>' +",
     "(i.options_text ? '<div class=\"cp-opts\">' + escapeHtml(i.options_text) + '</div>' : '') +\n                '<div class=\"cp-price\">' + formatMoney(i.price) + '</div>' +"),
    ("panel buttons",
     "? postForm(urlFor(CART_URLS.remove, id))\n            : postForm(urlFor(CART_URLS.update, id), {action: action});",
     "? postForm(urlFor(CART_URLS.remove, id), {line: itemEl.dataset.line || ''})\n            : postForm(urlFor(CART_URLS.update, id), {action: action, line: itemEl.dataset.line || ''});"),
]
js_re = re.compile(r"<script>\n    // Item popup: opens when an ADD button is clicked.*?\n</script>\n", re.S)

problems = []
for label, old, new in simple:
    n = s.count(old)
    if n != 1:
        problems.append(label + " (found " + str(n) + " times)")
if len(js_re.findall(s)) != 1:
    problems.append("old popup script")

if problems:
    print("Nothing changed. Could not match: " + "; ".join(problems))
    raise SystemExit

shutil.copy(p, str(p) + ".bak_options")
for label, old, new in simple:
    s = s.replace(old, new, 1)
s = js_re.sub(lambda m: NEW_JS, s, count=1)
p.write_text(s, encoding="utf-8")
print("menu_list.html updated with the option popup")
