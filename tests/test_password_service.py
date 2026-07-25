from app.services.password_service import PasswordService


def test_password_hashing():
    service = PasswordService()
    raw_password = "SuperSecretPassword123!"

    hashed = service.hash_password(raw_password)
    assert hashed != raw_password
    assert service.verify_password(raw_password, hashed) is True
    assert service.verify_password("WrongPassword123!", hashed) is False


def test_different_hashes_for_same_password():
    service = PasswordService()
    raw_password = "SuperSecretPassword123!"

    hash1 = service.hash_password(raw_password)
    hash2 = service.hash_password(raw_password)

    assert hash1 != hash2
    assert service.verify_password(raw_password, hash1) is True
    assert service.verify_password(raw_password, hash2) is True
