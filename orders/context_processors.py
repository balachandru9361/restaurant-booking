def cart_summary(request):
    """Adds cart_count (total quantity) to every template."""
    cart = request.session.get('cart', {}) or {}
    count = 0
    for v in cart.values():
        if isinstance(v, dict):
            q = v.get('quantity', v.get('qty', 1))
        else:
            q = v
        try:
            count += int(q)
        except (TypeError, ValueError):
            count += 1
    return {'cart_count': count}
