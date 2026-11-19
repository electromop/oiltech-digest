"""Конверт пакета формата версии 2 для потребителя выпуска (#198, спека #197).

Пакет подписан ключом платформы (Ed25519) и зашифрован на открытый ключ потребителя (HPKE, RFC 9180). Вскрывает его
двойник потребителя (tests/exchange_consumer.py), написанный по описанию формата, а не по коду платформы. Ключи лежат
в настройках ядра: несколько ключей одного вида действуют вместе, подписывает и шифрует первый."""

import base64
import json
import re

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from oiltech_digest import config, exchange_envelope
from tests.exchange_consumer import LABEL, Consumer, check_package, key_id, sample


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _signing_public(private: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()


@pytest.fixture()
def platform(monkeypatch):
    """Ключ подписи платформы и потребитель с его ключом в настройках ядра."""
    signing = exchange_envelope.new_signing_key()
    consumer = Consumer(_signing_public(signing))
    monkeypatch.setattr(config, "EXCHANGE_SIGNING_KEYS", _b64(signing))
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_KEYS", _b64(consumer.public_key))
    return signing, consumer


def test_shared_sample_is_a_v2_release():
    content = sample()
    check_package(content)
    assert content["kind"] == "release"
    assert {card["hypothesis"] for card in content["cards"]} >= {None, ""}


def test_consumer_opens_the_package_with_the_platform_key_and_its_own(platform):
    signing, consumer = platform
    content = sample()
    payload = json.dumps(content, ensure_ascii=False).encode("utf-8")

    data = exchange_envelope.seal(payload, *exchange_envelope.sending_keys())

    assert consumer.open(data) == content


def test_envelope_is_json_with_exactly_the_format_fields(platform):
    signing, consumer = platform

    envelope = json.loads(exchange_envelope.seal(b'{"x": 1}', *exchange_envelope.sending_keys()))

    assert set(envelope) == {"format", "version", "signer", "recipient", "ciphertext", "signature"}
    assert (envelope["format"], envelope["version"]) == ("digest-exchange-package", 2)
    assert envelope["signer"] == key_id(_signing_public(signing))
    assert envelope["recipient"] == key_id(consumer.public_key)
    for name in ("ciphertext", "signature"):
        assert re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", envelope[name]) and len(envelope[name]) % 4 == 0
    # В шифротексте enc (32 байта) и сразу шифротекст: короче 32 байт плюс метка AEAD он быть не может.
    assert len(base64.b64decode(envelope["ciphertext"])) == 32 + len(b'{"x": 1}') + 16


def test_signature_covers_the_format_label_and_the_ciphertext(platform):
    signing, consumer = platform
    envelope = json.loads(exchange_envelope.seal(b"{}", *exchange_envelope.sending_keys()))
    ciphertext = base64.b64decode(envelope["ciphertext"])
    signature = base64.b64decode(envelope["signature"])
    verify = Ed25519PublicKey.from_public_bytes(_signing_public(signing)).verify

    verify(signature, LABEL + ciphertext)
    with pytest.raises(InvalidSignature):
        verify(signature, b"oiltech-package/1" + ciphertext)
    with pytest.raises(InvalidSignature):
        verify(signature, LABEL + ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))


def test_first_key_of_each_kind_signs_and_encrypts_others_stay_valid(monkeypatch):
    """Смена ключей без простоя: старый и новый вместе; пакет называет свой ключ отпечатком."""
    new, old = exchange_envelope.new_signing_key(), exchange_envelope.new_signing_key()
    consumer_new, consumer_old = Consumer(_signing_public(new)), Consumer(_signing_public(new))
    monkeypatch.setattr(config, "EXCHANGE_SIGNING_KEYS", f"{_b64(new)}, {_b64(old)}")
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_KEYS", f"{_b64(consumer_new.public_key)},{_b64(consumer_old.public_key)}")

    data = exchange_envelope.seal(b"{}", *exchange_envelope.sending_keys())

    assert json.loads(data)["signer"] == key_id(_signing_public(new))
    assert json.loads(data)["recipient"] == key_id(consumer_new.public_key)
    described = exchange_envelope.describe()
    assert described["signing"] == [key_id(_signing_public(new)), key_id(_signing_public(old))]
    assert described["consumer"] == [key_id(consumer_new.public_key), key_id(consumer_old.public_key)]
    assert described["problems"] == []


@pytest.mark.parametrize("signing, consumer, reason", [
    ("", "CONSUMER", "нет ключа подписи платформы (EXCHANGE_SIGNING_KEYS)"),
    ("SIGNING", "", "нет открытого ключа потребителя (EXCHANGE_CONSUMER_KEYS)"),
    ("не-base64!", "CONSUMER", "EXCHANGE_SIGNING_KEYS: ключ 1 не в base64"),
    ("SIGNING", "SIGNING,AAAA", "EXCHANGE_CONSUMER_KEYS: ключ 2 не 32 байта"),
])
def test_without_keys_sending_is_refused_with_a_reason(monkeypatch, signing, consumer, reason):
    good = {"SIGNING": _b64(exchange_envelope.new_signing_key()), "CONSUMER": _b64(Consumer(b"\0" * 32).public_key)}
    signing = ",".join(good.get(part, part) for part in signing.split(",")) if signing else ""
    consumer = ",".join(good.get(part, part) for part in consumer.split(",")) if consumer else ""
    monkeypatch.setattr(config, "EXCHANGE_SIGNING_KEYS", signing)
    monkeypatch.setattr(config, "EXCHANGE_CONSUMER_KEYS", consumer)

    with pytest.raises(exchange_envelope.NotConfigured) as refused:
        exchange_envelope.sending_keys()

    assert str(refused.value) == reason
    assert good["SIGNING"] not in str(refused.value)
    assert reason in exchange_envelope.describe()["problems"]


def test_new_key_pairs_print_the_public_part_with_its_fingerprint():
    for kind in ("signing", "decryption"):
        pair = exchange_envelope.new_key_pair(kind)
        private, public = base64.b64decode(pair["private"]), base64.b64decode(pair["public"])
        assert len(private) == len(public) == 32
        assert pair["fingerprint"] == key_id(public)
        assert pair["setting"] == {"signing": "EXCHANGE_SIGNING_KEYS", "decryption": "EXCHANGE_DECRYPTION_KEYS"}[kind]
    signing = exchange_envelope.new_key_pair("signing")
    assert base64.b64decode(signing["public"]) == _signing_public(base64.b64decode(signing["private"]))
