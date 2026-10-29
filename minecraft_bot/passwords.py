import base64
import hashlib
import secrets


ITERATIONS = 310_000
SALT_BYTES = 16
HASH_BYTES = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, ITERATIONS, dklen=HASH_BYTES
    )
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"pbkdf2_sha256${ITERATIONS}${salt_text}${digest_text}"
