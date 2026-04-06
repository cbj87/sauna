"""
Generate VAPID key pair for Web Push notifications.
Run once and add the output to your .env file:

    python generate_vapid_keys.py
"""
import base64

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import ec


def main():
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())

    # Private key — raw 32-byte scalar, base64url-encoded (no padding)
    private_int = private_key.private_numbers().private_value
    private_bytes = private_int.to_bytes(32, "big")
    private_b64 = base64.urlsafe_b64encode(private_bytes).rstrip(b"=").decode()

    # Public key — uncompressed point (0x04 || x || y), base64url-encoded
    pub_numbers = private_key.public_key().public_numbers()
    x = pub_numbers.x.to_bytes(32, "big")
    y = pub_numbers.y.to_bytes(32, "big")
    public_bytes = b"\x04" + x + y
    public_b64 = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode()

    print("Add these to your .env file:\n")
    print(f"VAPID_PRIVATE_KEY={private_b64}")
    print(f"VAPID_PUBLIC_KEY={public_b64}")


if __name__ == "__main__":
    main()
