# accounts/permissions.py
from datetime import timedelta

from django.utils import timezone
from rest_framework.permissions import BasePermission

from billing.exceptions import StaffOverPlanLimit
from billing.plans import staff_access_blocked
from business.utils import get_user_business
from .models import AccountStatus


class BusinessActivePermission(BasePermission):
    """
    Runs on every authenticated request (wired in as a default permission
    class, see settings.py). Blocks the owner themselves and every
    staff member on their business the moment the owner is SUSPENDED,
    without touching each StaffMember's own status field, see the
    comment in services.set_account_status for why. A view that declares
    its own permission_classes (login, register, password reset) bypasses
    this the same way it bypasses IsAuthenticated.
    """
    message = "This business account is currently suspended."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return True

        business = get_user_business(user)
        if business is None:
            return True

        return business.owner.status != AccountStatus.SUSPENDED


class StaffWithinPlanPermission(BasePermission):
    """
    Blocks the newest staff seats over the plan's staff cap on every request,
    so an access token issued before a downgrade stops working too. Owners and
    unauthenticated requests pass straight through. A view that declares its own
    permission_classes must list this class, the same as BusinessActivePermission.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return True
        if staff_access_blocked(user):
            raise StaffOverPlanLimit()
        return True


def is_managerial_staff(user) -> bool:
    """True when this user is an active staff member with managerial access."""
    # Imported here so accounts.permissions never creates an import cycle with staff.
    from staff.models import StaffMember

    business = get_user_business(user)
    if business is None:
        return False
    return StaffMember.objects.filter(
        user=user,
        business=business,
        status='active',
        permission_level='managerial_access',
    ).exists()


class StaffProductAccessPermission(BasePermission):
    """
    Product endpoint rules for staff. Owners and superusers pass straight through.

    Nobody on staff can edit a product or manually adjust its stock. Creating a
    product needs managerial access. The one exception to "no editing" is the
    picture: the web form uploads a new product's image after the product exists
    and saves it with a PATCH, so a managerial staff member may PATCH image_url
    alone, and only within IMAGE_WINDOW of the product being created.

    Reads and bulk-deduct pass through here. The invoice flow calls bulk-deduct
    for every staff member, and delete and deactivate are already owner only
    inside the view.
    """
    message = "You do not have permission to do that."

    EDIT_ACTIONS = {'update', 'adjust_stock'}
    IMAGE_FIELDS = {'image_url', 'imageUrl'}
    IMAGE_WINDOW = timedelta(minutes=10)

    @staticmethod
    def _is_privileged(user):
        return getattr(user, 'role', None) == 'owner' or user.is_superuser

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return True
        if self._is_privileged(user):
            return True

        action = getattr(view, 'action', None)

        if action in self.EDIT_ACTIONS:
            self.message = "Staff cannot change products or adjust stock. Ask the business owner."
            return False

        if action in ('create', 'partial_update') and not is_managerial_staff(user):
            self.message = "Adding products needs managerial access. Ask the business owner."
            return False

        # partial_update for a manager is settled in has_object_permission.
        return True

    def has_object_permission(self, request, view, obj):
        user = request.user
        if self._is_privileged(user):
            return True
        if getattr(view, 'action', None) != 'partial_update':
            return True

        keys = set(request.data.keys())
        image_only = bool(keys) and keys <= self.IMAGE_FIELDS
        just_created = timezone.now() - obj.created_at <= self.IMAGE_WINDOW
        if image_only and just_created:
            return True

        self.message = "Staff cannot change products or adjust stock. Ask the business owner."
        return False