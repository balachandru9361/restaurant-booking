import uuid


QR_SESSION_COOKIE = 'qr_session_token'


def storage_key(request):
    token = request.COOKIES.get(QR_SESSION_COOKIE)
    if token:
        try:
            return f'qr_cart_{uuid.UUID(token)}'
        except (TypeError, ValueError, AttributeError):
            pass
    return 'cart'


def get_cart(request):
    return request.session.get(storage_key(request), {})


def save_cart(request, cart):
    request.session[storage_key(request)] = cart
    request.session.modified = True


def clear_cart(request):
    request.session.pop(storage_key(request), None)
    request.session.modified = True
