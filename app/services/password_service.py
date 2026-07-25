"""
Password hashing and verification using Argon2 via pwdlib.

Never log plaintext passwords, password hashes, or reset tokens.
"""

from pwdlib import PasswordHash


class PasswordService:
    """
    Thin wrapper around pwdlib.PasswordHash.

    Using ``PasswordHash.recommended()`` selects the best available
    algorithm (Argon2id) and automatically handles algorithm upgrades
    as the recommended defaults change.
    """

    def __init__(self) -> None:
        self.password_hash = PasswordHash.recommended()

    def hash_password(
        self,
        password: str,
    ) -> str:
        """Return an Argon2 hash of *password*."""
        return self.password_hash.hash(password)

    def verify_password(
        self,
        password: str,
        password_hash: str,
    ) -> bool:
        """Return True if *password* matches *password_hash*."""
        return self.password_hash.verify(
            password,
            password_hash,
        )
