# billing/exceptions.py
from rest_framework.exceptions import APIException

STAFF_BLOCKED_MESSAGE = (
    "Your business plan no longer includes your staff seat. "
    "Ask the business owner to upgrade or free up a seat."
)


class PlanLimitReached(APIException):
    """
    403 with a machine readable code. Frontends key off `code` and use
    `resource`, `limit` and `current` to build their own message.
    Both `error` and `detail` carry the text, matching DomainError.
    """
    status_code = 403
    default_code = 'plan_limit_reached'

    def __init__(self, *, resource, limit, current, plan, message):
        super().__init__(detail=message, code=self.default_code)
        # APIException turns every value into a string. Replace the payload so
        # limit and current stay numbers in the JSON the apps receive.
        self.detail = {
            'error': message,
            'detail': message,
            'code': self.default_code,
            'resource': resource,
            'limit': limit,
            'current': current,
            'plan': plan,
        }


class PlanFeatureLocked(APIException):
    """
    403 for a feature the current plan does not include, such as a premium
    invoice template. `allowed` lists what the plan does include.
    """
    status_code = 403
    default_code = 'plan_feature_locked'

    def __init__(self, *, feature, plan, message, allowed=None):
        super().__init__(detail=message, code=self.default_code)
        self.detail = {
            'error': message,
            'detail': message,
            'code': self.default_code,
            'feature': feature,
            'plan': plan,
            'allowed': list(allowed or []),
        }


class StaffOverPlanLimit(APIException):
    """Raised when a staff member is one of the newest seats over the plan's staff cap."""
    status_code = 403
    default_code = 'staff_over_plan_limit'

    def __init__(self, message=STAFF_BLOCKED_MESSAGE):
        super().__init__(detail=message, code=self.default_code)
        self.detail = {'error': message, 'detail': message, 'code': self.default_code}