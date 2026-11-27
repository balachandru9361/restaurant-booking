import logging

from django.conf import settings

try:
    import razorpay
except ImportError:
    razorpay = None

logger = logging.getLogger(__name__)


def refund_razorpay_payment(order, reason=''):
    """Full refund to the customer's original payment method. Returns (ok, error)."""
    payment_id = getattr(order, 'razorpay_payment_id', '')
    if not payment_id:
        return False, 'No Razorpay payment id saved for this order.'
    if payment_id.startswith('mock_'):
        return True, ''
    if razorpay is None:
        return False, 'razorpay library is not installed.'
    key_id = getattr(settings, 'RAZORPAY_KEY_ID', '')
    key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', '')
    if not key_id or not key_secret:
        return False, 'Razorpay keys are missing in settings.'
    try:
        client = razorpay.Client(auth=(key_id, key_secret))
        client.payment.refund(payment_id, {
            'amount': int(order.total_amount * 100),
            'notes': {'order_id': str(order.id), 'reason': (reason or 'Order rejected')[:200]},
        })
        return True, ''
    except Exception as exc:
        logger.exception('Razorpay refund failed for order %s', order.id)
        return False, str(exc)[:200]
