"""
Test client for sms-triage, and the reference for how the phone app talks to it.

Each request is JSON with "ts" (unix seconds) and "rid" (a random id) added, encrypted with
AES-256-GCM using SMS_KEY and a fresh 12-byte nonce, with the endpoint path as associated data,
and posted as {"v": 1, "n": base64(nonce), "c": base64(ciphertext + tag)}. The response comes
back in the same envelope, with "<path>#<rid>" as associated data.

Inside the container (SMS_KEY is already set there):
    python sms_client.py incoming --number 0412345678 --text "Hi, can you quote to hang a door?"
    python sms_client.py incoming --number 0412345678 --name "Jan" --contact --text "Running late"
    python sms_client.py draft --number 0412345678 --name "Jan" --text "Are you free Thursday?"
    python sms_client.py review --number 0412345678 --name "Jan" --file conversation.json
    python sms_client.py forget --number 0412345678

conversation.json is a list of {"from_me": true/false, "body": "..."}, oldest first.
"""

import argparse
import base64
import json
import os
import sys
import time
import uuid

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def call(url, key, path, payload):
    aead = AESGCM(base64.b64decode(key))
    rid = uuid.uuid4().hex
    body = dict(payload, ts=int(time.time()), rid=rid)
    nonce = os.urandom(12)
    sealed = aead.encrypt(nonce, json.dumps(body).encode(), path.encode())
    envelope = {"v": 1, "n": base64.b64encode(nonce).decode(), "c": base64.b64encode(sealed).decode()}
    res = requests.post(url.rstrip("/") + path, json=envelope, timeout=150)
    if res.status_code in (401, 404, 413):
        return res.status_code, None
    reply = res.json()
    plain = aead.decrypt(base64.b64decode(reply["n"]), base64.b64decode(reply["c"]), f"{path}#{rid}".encode())
    return res.status_code, json.loads(plain)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["incoming", "draft", "review", "forget"])
    parser.add_argument("--url", default=os.environ.get("SMS_URL", "http://localhost:8080"))
    parser.add_argument("--number", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--contact", action="store_true", help="sender is in the phone's contacts")
    parser.add_argument("--text", help="the incoming message (incoming, draft)")
    parser.add_argument("--file", help="conversation JSON (review, or the thread for draft)")
    args = parser.parse_args()

    key = os.environ.get("SMS_KEY")
    if not key:
        sys.exit("SMS_KEY is not set")

    thread = json.load(open(args.file)) if args.file else []
    if args.text:
        thread.append({"from_me": False, "body": args.text})

    payload = {"number": args.number, "name": args.name}
    if args.command == "incoming":
        payload.update(is_contact=args.contact, body=args.text or "", thread=thread)
    elif args.command == "draft":
        payload.update(thread=thread)
    elif args.command == "review":
        payload.update(messages=thread)

    started = time.time()
    status, result = call(args.url, key, f"/sms/{args.command}", payload)
    print(f"HTTP {status} in {time.time() - started:.1f}s")
    if result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
