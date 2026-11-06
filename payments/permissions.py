from rest_framework.permissions import BasePermission

from business.utils import get_user_business
from staff.models import StaffMember


class CanManagePayments(BasePermission):
    """Owner, or active staff with managerial access. Also resolves the
    business once and caches it on the request for the view to use."""
    message = 'Only the business owner or managerial staff can manage payments.'

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        business = get_user_business(user)
        if business is None:
            return False
        allowed = business.owner_id == user.id or StaffMember.objects.filter(
            user=user, business=business, status='active', permission_level='managerial_access',
        ).exists()
        if allowed:
            request._payments_business = business
        return allowed


class IsBusinessOwner(BasePermission):
    message = 'Only the business owner can do this.'

    def has_permission(self, request, view):
        business = getattr(request, '_payments_business', None) or get_user_business(request.user)
        return business is not None and business.owner_id == request.user.id
    