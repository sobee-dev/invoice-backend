from django.core import signing
from django.utils import timezone

TERMS_VERSION = "2026-10"          # bump when the documents change
_SALT = "google-signup"
_MAX_AGE = 600                     # seconds the signup token stays valid


def terms_fields():
    return {"terms_accepted_at": timezone.now(), "terms_version": TERMS_VERSION}


def issue_signup_token(email, first_name, last_name):
    """Carries the Google-verified identity, so the single-use Google code isn't needed again."""
    return signing.dumps(
        {"email": email, "first_name": first_name, "last_name": last_name},
        salt=_SALT,
    )


def read_signup_token(token):
    """Raises signing.BadSignature (SignatureExpired is a subclass) if invalid or older than 10 min."""
    return signing.loads(token, salt=_SALT, max_age=_MAX_AGE)