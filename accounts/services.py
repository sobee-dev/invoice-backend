# accounts/services.py
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from notifications.models import PushToken

from .models import AccountStatus, AccountStatusAudit, User

DELETION_GRACE_PERIOD = timedelta(days=365)


@transaction.atomic
def set_account_status(*, user: User, new_status: str, reason: str, actor=None) -> User:
    """
    The only supported way to change a User's lifecycle status. Writes the
    status fields and an AccountStatusAudit row in the same transaction —
    call sites never need to remember to log the change themselves.
    """
    old_status = user.status
    if old_status == new_status:
        return user

    user.status = new_status
    user.status_reason = reason
    user.status_changed_at = timezone.now()

    if new_status == AccountStatus.PENDING_DELETION:
        user.deletion_scheduled_for = timezone.now() + DELETION_GRACE_PERIOD
        user.is_active = False
    elif new_status == AccountStatus.SUSPENDED:
        user.deletion_scheduled_for = None
        user.is_active = False
    elif new_status == AccountStatus.ACTIVE:
        user.deletion_scheduled_for = None
        user.is_active = True

    user.save(update_fields=[
        'status', 'status_reason', 'status_changed_at',
        'deletion_scheduled_for', 'is_active',
    ])

    AccountStatusAudit.objects.create(
        user_id=user.id,
        email=user.email,
        from_status=old_status,
        to_status=new_status,
        reason=reason,
        actor_id=getattr(actor, 'id', None),
        actor_email=getattr(actor, 'email', ''),
    )

    if new_status in (AccountStatus.PENDING_DELETION, AccountStatus.SUSPENDED):
        _blacklist_all_tokens(user)

    if new_status == AccountStatus.SUSPENDED and user.role == 'owner':
        business = getattr(user, 'business', None)
        if business:
            for staff in business.staff_members.select_related('user').all():
                _blacklist_all_tokens(staff.user)

    if new_status == AccountStatus.PENDING_DELETION:
        # Deferred via on_commit rather than sent inline: this function
        # runs inside @transaction.atomic, and firing a real network call
        # to Resend before we know the transaction actually committed
        # risks telling the user "your account is scheduled for deletion"
        # when the DB write could still roll back. on_commit only runs
        # after a successful commit, and send_account_deletion_scheduled_email
        # already swallows its own exceptions and returns False rather
        # than raising — so a failed send never blocks or undoes the
        # deletion itself, it just gets logged.
        scheduled_for = user.deletion_scheduled_for
        transaction.on_commit(lambda: _send_deletion_email(user, scheduled_for))
        
    elif new_status == AccountStatus.SUSPENDED:
        transaction.on_commit(lambda: _send_suspension_email(user, reason))    

    return user


def _send_deletion_email(user: User, scheduled_for) -> None:
    from accounts.email import send_account_deletion_scheduled_email
    send_account_deletion_scheduled_email(user, scheduled_for)
    
def _send_suspension_email(user: User, reason: str) -> None:
    from accounts.email import send_account_suspended_email
    send_account_suspended_email(user, reason)    


def _blacklist_all_tokens(user: User) -> None:
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
    for outstanding in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=outstanding)
        
def purge_account(user: User) -> None:
    """
    Called only by the scheduled purge job, only on accounts whose grace
    period has expired. Staff are hard-deleted — nothing financial hangs
    off a staff account. Owners are anonymized in place, never deleted,
    because Business.owner and Document.business both CASCADE: deleting
    the owner row would silently wipe every invoice tied to the business.
    """
    if user.role == 'staff':
        _hard_delete_staff(user)
    else:
        _anonymize_owner(user)


def _hard_delete_staff(user: User) -> None:
    # StaffMember.user is on_delete=CASCADE, so this takes the StaffMember
    # row with it automatically — nothing else references a staff User.
    user.delete()


def _anonymize_owner(user: User) -> None:
    from business.services import delete_cloudinary_asset

    business = getattr(user, 'business', None)  # OneToOne reverse accessor

    if business:
        if business.logo_url:
            delete_cloudinary_asset(business.logo_url)
        if business.signature_url:
            delete_cloudinary_asset(business.signature_url)
            
            
        for product in business.products.exclude(image_url=''):
            delete_cloudinary_asset(product.image_url)
            product.image_url = ''
            product.save(update_fields=['image_url'])

        business.name = f"Deleted Business ({business.id})"
        business.description = ''
        business.address_one = 'Redacted'
        business.address_two = None
        business.phone = '0000000'
        business.email = f'deleted-{business.id}@billbuzz.invalid'
        business.registration_number = None
        business.logo_url = None
        business.motto = None
        business.signature_type = 'none'   # Business.clean() clears signature_text/url for us
        business.save()

        # Owner's staff didn't ask for their account to be wiped — suspend
        # their login rather than delete their record, and log why.
        for staff in business.staff_members.select_related('user').all():
            if staff.user.status != AccountStatus.SUSPENDED:
                set_account_status(
                    user=staff.user,
                    new_status=AccountStatus.SUSPENDED,
                    reason=f'Owner account for {business.name} was purged.',
                )
    PushToken.objects.filter(user=user).delete()
    user.email = f'deleted-{user.id}@billbuzz.invalid'
    user.first_name = ''
    user.last_name = ''
    user.set_unusable_password()
    user.status = AccountStatus.DELETED
    user.save()        