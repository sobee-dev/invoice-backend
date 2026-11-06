from rest_framework.exceptions import APIException


class DomainError(APIException):
    """
    A business rule was broken (insufficient stock, overpayment, wrong status).
    Raised inside transaction.atomic blocks so everything rolls back, then DRF
    turns it into a JSON response. The body carries both `error` (what the
    mobile deduct modal reads) and `detail` (what getErrorMessage reads).
    """
    status_code = 400
    default_code = 'invalid'

    def __init__(self, message, code='invalid', status_code=None):
        if status_code is not None:
            self.status_code = status_code
        super().__init__(detail={'error': message, 'detail': message, 'code': code}, code=code)