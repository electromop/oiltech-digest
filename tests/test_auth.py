from oiltech_digest import auth


def test_hash_and_verify_password_roundtrip():
    salt_hex, password_hash = auth.hash_password("supersecret123")

    assert auth.verify_password("supersecret123", salt_hex, password_hash) is True
    assert auth.verify_password("wrongpass", salt_hex, password_hash) is False


def test_validate_email_and_password_rules():
    assert auth.validate_email("user@example.com") is True
    assert auth.validate_email("not-an-email") is False
    assert auth.validate_password("12345678") is True
    assert auth.validate_password("1234567") is False
