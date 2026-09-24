"""Which Polymarket account wallet a signing key controls, for each account type.

Polymarket account wallets are CREATE2 contracts whose address follows from the owner's address, so the funder address
in the config can be checked against the private key without any network call. Ported from Polymarket's official
client (`@polymarket/client`, packages/client/src/wallet.ts), with the same constants and byte layout as the Node bot's
src/wallets.ts, and tested against the same vectors.
"""
from __future__ import annotations

from typing import Any, Optional

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

WALLET_DERIVATION = {
    "depositWalletFactory": "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07",
    "depositWalletBeacon": "0x7A18EDfe055488A3128f01F563e5B479D92ffc3a",
    "depositWalletImplementation": "0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB",
    "proxyFactory": "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052",
    "proxyImplementation": "0x44e999d5c2F66Ef0861317f9A4805AC2e90aEB4f",
    "safeFactory": "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b",
    "safeInitCodeHash": "0x2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf",
}

_PROXY_BYTECODE_TEMPLATE = (
    "3d3d606380380380913d393d73%s5af4602a57600080fd5b602d8060366000396000f3363d3d373d3d3d363d73%s5af43d82803e903d91602b57fd5bf352e831dd"
    "00000000000000000000000000000000000000000000000000000000000000200000000000000000000000000000000000000000000000000000000000000000"
)
_ERC1967_CONST1 = "0xcc3735a920a3ca505d382bbc545af43d6000803e6038573d6000fd5b3d6000f3"
_ERC1967_CONST2 = "0x5155f3363d3d373d3d363d7f360894a13ba1a3210667c828492db98dca3e2076"
_ERC1967_PREFIX = 0x61003D3D8160233D3973
_BEACON_CONST1 = "0xb3582b35133d50545afa5036515af43d6000803e604d573d6000fd5b3d6000f3"
_BEACON_CONST2 = "0x1b60e01b36527fa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6c"
_BEACON_CONST3 = "0x60195155f3363d3d373d3d363d602036600436635c60da"
_BEACON_PREFIX = 0x6100523D8160233D3973


def _b(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str[2:] if hex_str.startswith("0x") else hex_str)


def _create2(factory: str, salt: bytes, init_code_hash: bytes) -> str:
    """CREATE2: keccak256(0xff ++ factory ++ salt ++ initCodeHash)[12:]"""
    return to_checksum_address(keccak(b"\xff" + _b(factory) + salt + init_code_hash)[12:])


def proxy_wallet_address(signer: str) -> str:
    c = WALLET_DERIVATION
    bytecode = _PROXY_BYTECODE_TEMPLATE.replace("%s", c["proxyFactory"].lower()[2:], 1).replace("%s", c["proxyImplementation"].lower()[2:], 1)
    return _create2(c["proxyFactory"], keccak(_b(signer)), keccak(_b(bytecode)))


def safe_wallet_address(signer: str) -> str:
    c = WALLET_DERIVATION
    return _create2(c["safeFactory"], keccak(encode(["address"], [to_checksum_address(signer)])), _b(c["safeInitCodeHash"]))


def _deposit_args(signer: str) -> bytes:
    wallet_id = _b(signer).rjust(32, b"\x00")
    return encode(["address", "bytes32"], [WALLET_DERIVATION["depositWalletFactory"], wallet_id])


def _init_code_hash(prefix_base: int, target: str, middle: list[str], args: bytes) -> bytes:
    prefix = (prefix_base + (len(args) << 56)).to_bytes(10, "big")
    return keccak(prefix + _b(target) + b"".join(_b(m) for m in middle) + args)


def beacon_deposit_wallet_address(signer: str) -> str:
    """the Deposit Wallet a signer owns (current beacon factory)"""
    args = _deposit_args(signer)
    h = _init_code_hash(_BEACON_PREFIX, WALLET_DERIVATION["depositWalletBeacon"], [_BEACON_CONST3, _BEACON_CONST2, _BEACON_CONST1], args)
    return _create2(WALLET_DERIVATION["depositWalletFactory"], keccak(args), h)


def uups_deposit_wallet_address(signer: str) -> str:
    """the Deposit Wallet a signer owns (earlier UUPS factory)"""
    args = _deposit_args(signer)
    h = _init_code_hash(_ERC1967_PREFIX, WALLET_DERIVATION["depositWalletImplementation"], ["0x6009", _ERC1967_CONST2, _ERC1967_CONST1], args)
    return _create2(WALLET_DERIVATION["depositWalletFactory"], keccak(args), h)


def wallets_of(signer: str) -> dict[int, list[str]]:
    """every account wallet a signer controls, by signatureType"""
    s = to_checksum_address(signer)
    return {0: [s], 1: [proxy_wallet_address(s)], 2: [safe_wallet_address(s)], 3: [beacon_deposit_wallet_address(s), uups_deposit_wallet_address(s)]}


def check_funder(signer: str, funder: Optional[str], signature_type: int) -> dict[str, Any]:
    """Does `funder` belong to `signer` as an account of `signature_type`? When it does not, `expected` is the address
    that would, and `actualType` is the type the funder does belong to, if any."""
    all_ = wallets_of(signer)
    f = (funder if funder is not None else signer).lower()
    expected = all_.get(signature_type, [])
    ok = any(a.lower() == f for a in expected)
    hit = next((t for t, lst in all_.items() if any(a.lower() == f for a in lst)), None)
    return {"ok": ok, "expected": expected, "actualType": hit}
