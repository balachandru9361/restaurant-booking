import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from booking.models import SeatAllocation, TableSession
from .models import Order

try:
    import razorpay
except ImportError:
    razorpay = None

logger = logging.getLogger(__name__)


@transaction.atomic
def release_cancelled_order_seats(order):
    """Release only a cancelled QR order's session seats when no other order remains."""
    if order.status != 'cancelled' or not order.table_session_id:
        return False

    session = (
        TableSession.objects.select_for_update()
        .filter(pk=order.table_session_id, source=TableSession.SOURCE_QR)
        .first()
    )
    if session is None:
        return False

    if Order.objects.filter(table_session_id=session.pk).exclude(status='cancelled').exists():
        return False

    session.allocations.select_for_update().filter(
        status__in=[SeatAllocation.HELD, SeatAllocation.OCCUPIED],
    ).update(status=SeatAllocation.RELEASED, held_until=None)

    if session.is_active:
        now = timezone.now()
        session.is_active = False
        session.closed_at = now
        session.status = TableSession.STATUS_CANCELLED
        session.save(update_fields=['is_active', 'closed_at', 'status'])
        session.bills.update(is_billed=True, is_closed=True, closed_at=now)
    return True


def process_order_refund(order, reason=''):
    """Attempt a pending online refund without changing the order's payment status."""
    if order.payment_method != 'online' or order.payment_status != 'paid':
        return False, ''
    if order.refund_status == 'refunded':
        return True, ''
    if order.refund_status == 'partially_refunded':
        return False, 'This order has already been partially refunded.'

    Order.objects.filter(pk=order.pk).exclude(
        refund_status__in=['refunded', 'partially_refunded']
    ).update(refund_status='pending')
    order.refresh_from_db(fields=['refund_status'])
    if order.refund_status == 'refunded':
        return True, ''
    if order.refund_status == 'partially_refunded':
        return False, 'This order has already been partially refunded.'

    ok, error = refund_razorpay_payment(order, reason)
    if ok:
        Order.objects.filter(pk=order.pk).update(refund_status='refunded')
        order.refund_status = 'refunded'
    else:
        order.refund_status = 'pending'
    return ok, error


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
