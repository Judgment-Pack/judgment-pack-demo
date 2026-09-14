#!/usr/bin/env python3
"""A stand-in identity provider for demo Act 8.

The engine verifies bearer tokens against a local copy of an issuer's public
keys (engine-config.md, `identity`): a JWT in compact form, signed with a key
the file holds under its kid, from the configured issuer, naming the engine as
audience, within its validity window. This script is the issuer the demo has
in place of a company's own: `keygen` mints one P-256 key pair and writes the
public half as the key set the engine reads; `mint` signs a token for a named
person. What a token proves is who asked -- never that they approved a
particular action, which the plan leaves open and the receipt does not claim.
"""
import argparse
import base64
import json
import os
import sys
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

KID = "demo-2026-09"


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def keygen(directory):
    os.makedirs(directory, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    private = os.path.join(directory, "issuer.key")
    with open(private, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(private, 0o600)
    numbers = key.public_key().public_numbers()
    jwk = {
        "kid": KID,
        "kty": "EC",
        "crv": "P-256",
        "x": b64(numbers.x.to_bytes(32, "big")),
        "y": b64(numbers.y.to_bytes(32, "big")),
        "use": "sig",
        "alg": "ES256",
    }
    with open(os.path.join(directory, "keys.json"), "w") as f:
        json.dump({"keys": [jwk]}, f, indent=2)
    print("issuer key pair written under %s; the engine reads keys.json" % directory)


def mint(directory, subject, issuer, audience, ttl):
    with open(os.path.join(directory, "issuer.key"), "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    now = int(time.time())
    header = {"alg": "ES256", "typ": "JWT", "kid": KID}
    claims = {"iss": issuer, "aud": audience, "sub": subject, "iat": now, "nbf": now, "exp": now + ttl}
    signing = b64(json.dumps(header, separators=(",", ":")).encode()) + "." + b64(json.dumps(claims, separators=(",", ":")).encode())
    der = key.sign(signing.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    print(signing + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big")))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("directory")
    m = sub.add_parser("mint")
    m.add_argument("directory", help="where keygen put issuer.key; /private/issuer in the engine container")
    m.add_argument("--subject", required=True, help="who is asking, as the identity provider names them")
    m.add_argument("--issuer", default="https://issuer.enterprise-demo.invalid")
    m.add_argument("--audience", default="gateway:enterprise-demo-engine")
    m.add_argument("--ttl", type=int, default=600)
    args = parser.parse_args()
    if args.command == "keygen":
        keygen(args.directory)
    else:
        mint(args.directory, args.subject, args.issuer, args.audience, args.ttl)


if __name__ == "__main__":
    main()
