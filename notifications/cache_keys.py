# notifications/cache_keys.py
def unread_count_cache_key(business_id):
    return f'unread_count:{business_id}'