# views.py
from django.http import JsonResponse
from django.db import connection

def health_check(request):
    try:
        # Perform a simple query to ensure DB is alive
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "healthy", "database": "connected"}, status=200)
    except Exception as e:
        return JsonResponse({"status": "unhealthy", "error": str(e)}, status=503)