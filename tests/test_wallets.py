import pytest

from pmwallets_copytrade.wallets import (
    beacon_deposit_wallet_address,
    check_funder,
    proxy_wallet_address,
    safe_wallet_address,
    uups_deposit_wallet_address,
)

# the vectors of Polymarket's own client (packages/client/src/wallet.test.ts), signer 0x…01
SIGNER = "0x0000000000000000000000000000000000000001"


@pytest.mark.parametrize("derive,expected", [
    (beacon_deposit_wallet_address, "0x94bf330955a0b957662feaf878de77bf25f76cd9"),
    (uups_deposit_wallet_address, "0x57ffbc34de23124faeb8387fcd689d314e57accd"),
    (proxy_wallet_address, "0x7754536ecd85c00b2e0cf9c1aa679340d8550756"),
    (safe_wallet_address, "0x766b6851a199bf91ae3fa13b1cfac5187355118f"),
], ids=["beacon Deposit Wallet", "UUPS Deposit Wallet", "Proxy Wallet", "Safe Wallet"])
def test_account_wallet_derivation(derive, expected):
    assert derive(SIGNER).lower() == expected


def test_accepts_the_funder_the_key_controls_as_that_type():
    assert check_funder(SIGNER, "0x94BF330955A0B957662FEAF878DE77BF25F76CD9", 3)["ok"]
    assert check_funder(SIGNER, "0x57ffbc34de23124faeb8387fcd689d314e57accd", 3)["ok"]
    assert check_funder(SIGNER, "0x766b6851a199bf91ae3fa13b1cfac5187355118f", 2)["ok"]
    assert check_funder(SIGNER, None, 0)["ok"]


def test_rejects_a_funder_of_another_type_and_names_it():
    r = check_funder(SIGNER, "0x766b6851a199bf91ae3fa13b1cfac5187355118f", 3)
    assert r["ok"] is False and r["actualType"] == 2


def test_rejects_a_funder_the_key_does_not_control():
    r = check_funder(SIGNER, "0x0000000000000000000000000000000000000002", 3)
    assert r["ok"] is False and r["actualType"] is None
    assert "0x94bf330955a0b957662feaf878de77bf25f76cd9" in [a.lower() for a in r["expected"]]
