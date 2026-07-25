import hashlib
import secrets

ADMIN_PREFIX = "tg_admin_"


def main() -> None:
    plaintext_key = ADMIN_PREFIX + secrets.token_urlsafe(32)

    key_hash = hashlib.sha256(plaintext_key.encode("utf-8")).hexdigest()

    print()
    print("Administrator API key:")
    print(plaintext_key)

    print()
    print("Add this hash to .env:")
    print(f"ADMIN_API_KEY_HASH={key_hash}")

    print()
    print("Save the plaintext key securely. It cannot be recovered from the hash.")


if __name__ == "__main__":
    main()
