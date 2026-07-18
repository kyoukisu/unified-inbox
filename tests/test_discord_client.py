from discord_adapter.client import (
    discord_nonce_for_idempotency_key,
    discord_nonce_value,
)


def test_discord_nonce_fits_signed_int64_and_is_deterministic() -> None:
    failed_key = "telegram:179535368"

    nonce = discord_nonce_for_idempotency_key(failed_key)

    assert nonce == discord_nonce_for_idempotency_key(failed_key)
    assert 0 <= nonce <= (1 << 63) - 1


def test_discord_nonce_retains_key_distinction() -> None:
    assert discord_nonce_for_idempotency_key(
        "telegram:179535367"
    ) != discord_nonce_for_idempotency_key("telegram:179535368")


def test_discord_gateway_string_nonce_is_normalized() -> None:
    assert discord_nonce_value("4949912097577381323") == 4949912097577381323
    assert discord_nonce_value(4949912097577381323) == 4949912097577381323
    assert discord_nonce_value("not-a-nonce") is None
