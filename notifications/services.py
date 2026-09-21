import logging
from datetime import timedelta
from typing import Iterable, Optional

import requests
from django.utils import timezone
from djangorestframework_camel_case.util import camelize

from .models import PushToken, Notification

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send'
EXPO_BATCH_SIZE = 100  # Expo rejects requests with more than 100 messages


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def send_push_to_tokens(
    tokens: Iterable[str], title: str, body: str, data: Optional[dict] = None
) -> bool:
    """
    Low-level send: batches messages and posts them to Expo's push API.
    Returns True only if every batch was *accepted* by Expo — this
    confirms Expo received the request, not that the notification was
    actually delivered to the device. Delivery confirmation requires
    polling Expo's separate receipt endpoint, which isn't implemented
    here; add it if you need delivery guarantees rather than best-effort.

    NOTE: `data` is camelCased before it leaves Django. Push payloads go
    straight from here to Expo/the device and never pass through
    CamelCaseJSONRenderer, so any snake_case keys a caller passes in
    (document_id, dedup_key, etc.) would otherwise reach the frontend
    as-is and read as `undefined` there.
    """
    tokens = [t for t in dict.fromkeys(tokens) if t]  # de-dupe, keep order
    if not tokens:
        return True

    push_data = camelize(data) if data else {}

    messages = [
        {'to': t, 'title': title, 'body': body, 'data': push_data, 'sound': 'default'}
        for t in tokens
    ]

    all_ok = True
    for batch in _chunk(messages, EXPO_BATCH_SIZE):
        try:
            resp = requests.post(
                EXPO_PUSH_URL,
                json=batch,
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                timeout=10,
            )
            resp.raise_for_status()
            tickets = resp.json().get('data', [])

            # A DeviceNotRegistered error means the token is dead (app
            # uninstalled, etc.) — prune it opportunistically so future
            # sends don't keep paying for a doomed request.
            for ticket, sent_token in zip(tickets, [m['to'] for m in batch]):
                details = ticket.get('details') or {}
                if ticket.get('status') == 'error' and details.get('error') == 'DeviceNotRegistered':
                    PushToken.objects.filter(token=sent_token).delete()
                    logger.info(f"Pruned dead push token: {sent_token}")
        except requests.RequestException as e:
            all_ok = False
            logger.error(f"Push batch failed to send: {e}")

    return all_ok


def send_push_to_user(user, title: str, body: str, data: Optional[dict] = None) -> bool:
    """Sends to every device currently registered for this user."""
    tokens = list(PushToken.objects.filter(user=user).values_list('token', flat=True))
    return send_push_to_tokens(tokens, title, body, data)


def send_push_to_business_owner(
    business, title: str, body: str, data: Optional[dict] = None
) -> bool:
    """Convenience wrapper for the common "notify the owner" case."""
    owner = getattr(business, 'owner', None)
    if owner is None:
        logger.warning(f"send_push_to_business_owner: no owner resolvable for business {business}")
        return False
    return send_push_to_user(owner, title, body, data)


# ─── Dispatch layer ─────────────────────────────────────────────────────────
#
# notify() is the single entry point every notification trigger should
# call — event-driven (a save() hook, a view action) or scheduled (a
# management command / cron job). Nothing outside this function should
# create a Notification row or call send_push_* directly.

def notify(
    business,
    notification_type: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    dedup_key: Optional[str] = None,
    cooldown_hours: Optional[float] = None,
    user=None,
) -> Optional[Notification]:
    """
    Persists a Notification (source of truth for the in-app notification
    center) unconditionally — that row is created regardless of role or
    push preference, so it's always available if you later decide to
    surface it somewhere else. The push side channel below is where
    the owner-only restriction and the push_notifications toggle are
    actually enforced.

    The Notification.data field is stored as passed in (snake_case is
    fine here — it's camelCased automatically by CamelCaseJSONRenderer
    whenever it's read back through a DRF endpoint). The push `data`
    sent to Expo is a separate, camelCased copy — see send_push_to_tokens.

    OWNER-ONLY: the whole notification system is scoped to business
    owners. target_user defaults to business.owner (always an owner by
    the data model). The `user=` override exists for targeting a
    specific device set in the future, but is still checked against
    role='owner' here — this is the one place that check needs to be
    correct, rather than trusting every future caller to remember it.

    dedup_key + cooldown_hours (optional throttle):
    If a Notification of the same (business, notification_type,
    dedup_key) was already created within the cooldown window, this
    call is a no-op and returns None. Scope dedup_key to the specific
    entity involved — a product id for low-stock, an invoice id for
    overdue — so throttling one product's alerts doesn't suppress
    another's.
    """
    if dedup_key and cooldown_hours:
        cutoff = timezone.now() - timedelta(hours=cooldown_hours)
        already_sent = Notification.objects.filter(
            business=business,
            notification_type=notification_type,
            data__dedup_key=dedup_key,
            created_at__gte=cutoff,
        ).exists()
        if already_sent:
            return None

    payload = dict(data or {})
    if dedup_key:
        payload['dedup_key'] = dedup_key

    notification = Notification.objects.create(
        business=business,
        notification_type=notification_type,
        title=title,
        body=body,
        data=payload,
    )

    target_user = user if user is not None else getattr(business, 'owner', None)

    push_ok = False
    if target_user is None:
        logger.warning(f"notify(): no target user resolvable for business {business.id}")
    elif target_user.role != 'owner':
        logger.warning(
            f"notify(): refused to push to non-owner user {target_user.email} "
            f"(role={target_user.role}) — notifications are owner-only."
        )
    elif not getattr(target_user, 'push_notifications', True):
        logger.debug(f"notify(): push skipped for {target_user.email} — push_notifications disabled")
    else:
        try:
            push_ok = send_push_to_user(target_user, title, body, payload)
        except Exception as e:
            logger.error(f"notify(): push send raised for business {business.id}: {e}")
            push_ok = False

    if push_ok:
        notification.push_sent = True
        notification.save(update_fields=['push_sent'])

    return notification