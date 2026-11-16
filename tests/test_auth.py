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


def test_validate_email_accepts_ordinary_corporate_addresses():
    for email in ("Ivan.Petrov@Example.ru", " first.last+radar@mail.sub-domain.example.com ", "o_t-1%x@a1.io"):
        assert auth.validate_email(email) is True, email


def test_validate_email_rejects_markup_quotes_control_chars_and_overlong():
    """Регистрация открыта (16.11): почту вводит посторонний, а она уходит в Telegram подтверждающим, в вывод
    команды на сервере и на экраны."""
    rejected = (
        "<script>@example.com", "a'b@example.com", 'a"b@example.com', "a;b@example.com", "a@example.com>",
        "a\x1b[31m@example.com", "a\x00@example.com", "a\u202e@example.com", "иван@example.com", "a@пример.рф",
        "a@example", "a@-example.com", "a@example.c", "a@@example.com", "a@exa mple.com",
        "x" * 65 + "@example.com", "a@" + "b" * 250 + ".com",
    )
    for email in rejected:
        assert auth.validate_email(email) is False, repr(email)
