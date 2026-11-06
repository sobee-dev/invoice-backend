# accounts/permissions.py
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