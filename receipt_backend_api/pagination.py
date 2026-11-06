# receipt_backend_api/pagination.py
from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """
    20 rows per page, selected with ?page=N. The response carries count,
    next, previous and results, which is the shape every client already
    reads. The page size is fixed on purpose so a client cannot ask for
    thousands of rows in one request.
    """
    page_size = 20
    page_size_query_param = None
    max_page_size = 20