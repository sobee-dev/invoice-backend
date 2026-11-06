# billing/permissions.py
from rest_framework import permissions


class IsBusinessOwner(permissions.BasePermission):
    """Billing management (checkout and portal links) is owner-only.
    Staff can read status but never touch billing controls."""
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == 'owner')