import pathlib

p = pathlib.Path("templates/menu/menu_list.html")
s = p.read_text(encoding="utf-8")

if "id=\"itemModal\"" in s:
    print("menu_list.html already has the item modal")
    raise SystemExit

FORM_OLD = 'class="add-to-cart-form" data-item-id="{{ item.id }}">'
FORM_NEW = 'class="add-to-cart-form" data-item-id="{{ item.id }}" data-desc="{{ item.description|default:\'\' }}">'

CSS_ANCHOR = "<!-- ===== WELCOME SPLASH ===== -->"
HTML_ANCHOR = '<div class="cart-toast" id="cartToast"></div>'

MODAL_CSS = """<style id="itemModalStyle">
    /* ===== ITEM POPUP (opens when ADD is clicked) ===== */
    html.im-lock, html.im-lock body { overflow: hidden !important; }
    .im-overlay {
        position: fixed; top: 0; left: 0; right: 0; bottom: 0;
        z-index: 9990;
        background: rgba(11, 20, 38, 0.55);
        display: none;
        align-items: center;
        justify-content: center;
        padding: 16px;
    }
    .im-overlay.open { display: flex; }
    .im-box {
        position: relative;
        width: 100%;
        max-width: 460px;
        max-height: 92vh;
        background: #fff;
        border-radius: 22px;
        overflow: hidden;
        display: flex;
        flex-direction: column;
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.35);
        animation: fadeUpIn 0.2s ease both;
    }
    .im-close {
        position: absolute; top: 12px; right: 12px; z-index: 2;
        width: 36px; height: 36px;
        border: none; border-radius: 50%;
        background: rgba(255, 255, 255, 0.95);
        color: #0b1426;
        font-size: 1.5rem; line-height: 1;
        cursor: pointer;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
    }
    .im-img { width: 100%; height: 230px; object-fit: cover; display: block; background: #efe9db; flex: none; }
    .im-body { padding: 18px 20px 8px; overflow-y: auto; }
    .im-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
    .im-head h3 { margin: 0; font-size: 1.3rem; font-weight: 800; color: #1b2233; line-height: 1.25; }
    .im-desc { margin: 0 0 16px; color: #5b6475; font-size: 0.92rem; line-height: 1.55; }
    .im-qtyrow {
        display: flex; align-items: center; justify-content: space-between;
        padding: 12px 0; border-top: 1px dashed #e3dccb;
        font-weight: 700; color: #1b2233;
    }
    .im-qty {
        display: inline-flex; align-items: center;
        border: 2px solid #0b1426; border-radius: 999px; overflow: hidden;
        background: #fffaf0;
    }
    .im-qty button {
        width: 42px; height: 40px; border: none; background: transparent;
        font-size: 1.2rem; font-weight: 800; color: #0b1426; cursor: pointer;
    }
    .im-qty button:hover { background: #f3cf7a; }
    .im-qty button:disabled { opacity: 0.4; cursor: not-allowed; }
    .im-qty span { min-width: 30px; text-align: center; font-weight: 800; }
    .im-foot {
        display: flex; align-items: center; justify-content: space-between; gap: 14px;
        padding: 14px 20px 18px; border-top: 1px solid #eee; background: #fff; flex: none;
    }
    .im-total small { display: block; color: #8a93a6; font-size: 0.72rem; font-weight: 700; }
    .im-total strong { font-size: 1.3rem; font-weight: 800; color: #0b1426; }
    .im-add {
        flex: 1; max-width: 230px; height: 48px;
        border: none; border-radius: 999px;
        background: linear-gradient(135deg, #f3cf7a, #d9a441);
        color: #0b1426; font-weight: 800; font-size: 0.95rem; letter-spacing: 0.4px;
        cursor: pointer;
    }
    .im-add:disabled { opacity: 0.6; cursor: not-allowed; }
    @media (max-width: 500px) {
        .im-overlay { align-items: flex-end; padding: 0; }
        .im-box { max-width: none; border-radius: 22px 22px 0 0; max-height: 94vh; }
        .im-img { height: 200px; }
    }
</style>

"""

MODAL_HTML = """<div class="im-overlay" id="itemModal" aria-hidden="true">
    <div class="im-box" role="dialog" aria-modal="true" aria-labelledby="imName">
        <button type="button" class="im-close" id="imClose" aria-label="Close">&times;</button>
        <img class="im-img" id="imImg" src="" alt="">
        <div class="im-body">
            <div class="im-head">
                <span class="dt-ic dt-veg" id="imDot"><i></i></span>
                <h3 id="imName"></h3>
            </div>
            <p class="im-desc" id="imDesc"></p>
            <div class="im-qtyrow">
                <span>Quantity</span>
                <div class="im-qty">
                    <button type="button" id="imMinus" aria-label="Decrease">&minus;</button>
                    <span id="imQty">1</span>
                    <button type="button" id="imPlus" aria-label="Increase">+</button>
                </div>
            </div>
        </div>
        <div class="im-foot">
            <div class="im-total"><small>Total</small><strong id="imTotal"></strong></div>
            <button type="button" class="im-add" id="imAdd">ADD TO CART</button>
        </div>
    </div>
</div>

"""

MODAL_JS = """<script>
    // Item popup: opens when an ADD button is clicked, adds the chosen quantity to the cart.
    (function () {
        var overlay = document.getElementById('itemModal');
        if (!overlay) { return; }

        var MAX_QTY = 20;
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
            close: document.getElementById('imClose')
        };
        var current = null;
        var qty = 1;

        function refresh() {
            el.qty.textContent = qty;
            el.total.textContent = formatMoney(current.price * qty);
            el.minus.disabled = qty <= 1;
            el.plus.disabled = qty >= MAX_QTY;
        }

        function openModal(form) {
            var card = form.closest('.item-card');
            var box = card.querySelector('.item-select-checkbox');
            var img = card.querySelector('.menu-slide-img');
            var title = card.querySelector('h3');
            var rawPrice = box ? String(box.dataset.price) : '0';
            var isVeg = card.dataset.veg === '1';

            current = {
                form: form,
                price: parseFloat(rawPrice.replace(/,/g, '')) || 0
            };
            qty = 1;

            el.img.src = img ? img.src : NO_IMAGE;
            el.img.alt = title ? title.textContent : '';
            el.name.textContent = box && box.dataset.name ? box.dataset.name : (title ? title.textContent : '');
            el.dot.className = 'dt-ic ' + (isVeg ? 'dt-veg' : 'dt-non');
            el.desc.textContent = form.dataset.desc || 'Freshly prepared and served hot.';
            el.add.disabled = false;
            el.add.textContent = 'ADD TO CART';

            overlay.classList.add('open');
            overlay.setAttribute('aria-hidden', 'false');
            document.documentElement.classList.add('im-lock');
            refresh();
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

        el.minus.addEventListener('click', function () {
            if (qty > 1) { qty--; refresh(); }
        });
        el.plus.addEventListener('click', function () {
            if (qty < MAX_QTY) { qty++; refresh(); }
        });
        el.close.addEventListener('click', closeModal);
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) { closeModal(); }
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && overlay.classList.contains('open')) { closeModal(); }
        });

        el.add.addEventListener('click', function () {
            if (!current) { return; }
            el.add.disabled = true;
            el.add.textContent = 'Adding...';
            postForm(current.form.action, {quantity: qty})
                .then(function (data) {
                    if (data.success) {
                        renderCartPanel(data);
                        showToast(data.message || 'Item added to cart!');
                        closeModal();
                    } else {
                        el.add.disabled = false;
                        el.add.textContent = 'ADD TO CART';
                        showToast(data.error || 'Could not add item.', true);
                    }
                })
                .catch(function (err) {
                    console.error(err);
                    el.add.disabled = false;
                    el.add.textContent = 'ADD TO CART';
                    showToast('Failed to add item. Please try again.', true);
                });
        });
    })();
</script>

"""

problems = []
for label, text in (("form tag", FORM_OLD), ("splash comment", CSS_ANCHOR), ("toast div", HTML_ANCHOR)):
    n = s.count(text)
    if n != 1:
        problems.append(label + " found " + str(n) + " times (expected 1)")
end_idx = s.rfind("{% endblock %}")
if end_idx == -1:
    problems.append("closing endblock not found")

if problems:
    print("Nothing changed. " + "; ".join(problems))
    raise SystemExit

s = s.replace(FORM_OLD, FORM_NEW, 1)
s = s.replace(CSS_ANCHOR, MODAL_CSS + CSS_ANCHOR, 1)
s = s.replace(HTML_ANCHOR, MODAL_HTML + HTML_ANCHOR, 1)
end_idx = s.rfind("{% endblock %}")
s = s[:end_idx] + MODAL_JS + s[end_idx:]

p.write_text(s, encoding="utf-8")
print("menu_list.html updated with the item popup")
