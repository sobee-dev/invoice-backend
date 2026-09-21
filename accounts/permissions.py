from rest_framework.permissions import BasePermission

from business.utils import get_user_business
from .models import AccountStatus


class BusinessActivePermission(BasePermission):
    """
    Runs on every authenticated request (wired in as a default permission
    class — see settings.py below). Blocks the owner themselves and every
    staff member on their business the moment the owner is SUSPENDED,
    without touching each StaffMember's own status field — see the
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