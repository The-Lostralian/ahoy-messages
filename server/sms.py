"""
sms-triage: the server side of the Ahoy Messages SMS app.

The phone posts each incoming text here. This decides whether it is spam or advertising (the app
files those under Filtered, without a notification), alerts ntfy for quote requests and customer
messages, and writes reply drafts in the owner's style. A draft is only ever a suggestion: the phone
puts it in the reply box and nothing is sent without the owner.

Traffic comes in through Cloudflare's proxy, which can read plain HTTPS, so every request and
response body is encrypted with AES-256-GCM using SMS_KEY, a key only the phone and this server
have. Requests carry a timestamp and a random id; stale or replayed requests are refused.

Endpoints (POST, encrypted JSON):
    /sms/incoming   sort one incoming text: filtered?, category, whether a draft is worth asking for
    /sms/draft      write a reply draft for a conversation
    /sms/review     learn context and writing style from a whole conversation
    /sms/forget     delete what was learned from one number's conversation
GET /health         plain "ok", for Uptime Kuma

Only reviewed conversations are stored (in /state/sms); incoming texts and the recent messages
sent along for drafts are used and then forgotten. Message text is never logged.
"""

import base64
import hashlib
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import triage
from triage import env

log = logging.getLogger("sms-triage")

PORT = int(env("SMS_PORT", "8080"))
STATE_DIR = env("SMS_STATE_DIR", "/state/sms")
MAX_BODY = 2_000_000           # a reviewed conversation can be long; anything bigger is refused
MAX_SKEW = 300                 # seconds a request's timestamp may differ from the server clock
THREAD_MESSAGES = 20           # recent messages used for a draft
EXAMPLES_KEPT = 400            # the owner's texts kept as style examples, newest first
DRAFT_EXAMPLES = 15            # examples shown to the model per draft
OWNER = env("OWNER_NAME", "Owner")  # the phone's owner, as the model sees them in conversations


# ==================== ENCRYPTION ==================== #

class Refused(Exception):
    """Bad key, tampered, stale or replayed request: answered with a bare 401."""


class BadRequest(Exception):
    pass


def load_key():
    try:
        key = base64.b64decode(env("SMS_KEY", ""), validate=True)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise SystemExit("SMS_KEY must be 32 random bytes, base64 encoded (openssl rand -base64 32)")
    return AESGCM(key)


def seal(aead, obj, aad):
    nonce = os.urandom(12)
    data = aead.encrypt(nonce, json.dumps(obj).encode(), aad.encode())
    return json.dumps({"v": 1, "n": base64.b64encode(nonce).decode(), "c": base64.b64encode(data).decode()}).encode()


def unseal(aead, body, aad):
    try:
        envelope = json.loads(body)
        nonce = base64.b64decode(envelope["n"], validate=True)
        data = base64.b64decode(envelope["c"], validate=True)
        return json.loads(aead.decrypt(nonce, data, aad.encode()))
    except Exception:
        raise Refused()


class ReplayGuard:
    """Remembers request ids for longer than MAX_SKEW, so each request is accepted once."""

    def __init__(self):
        self.seen = {}
        self.lock = threading.Lock()

    def check(self, request):
        ts, rid = request.get("ts"), request.get("rid")
        if not isinstance(ts, (int, float)) or abs(time.time() - ts) > MAX_SKEW:
            raise Refused()
        if not isinstance(rid, str) or not 8 <= len(rid) <= 64:
            raise Refused()
        with self.lock:
            cutoff = time.time() - 2 * MAX_SKEW
            self.seen = {r: t for r, t in self.seen.items() if t > cutoff}
            if rid in self.seen:
                raise Refused()
            self.seen[rid] = time.time()


# ==================== NUMBERS AND STORAGE ==================== #

def normalise_number(number):
    """'+61 412 345 678' and '0412345678' are the same phone; sender names like 'Telstra' stay as they are."""
    number = str(number or "").strip()
    if re.search(r"[A-Za-z]", number):
        return number
    digits = re.sub(r"[^\d+]", "", number)
    if digits.startswith("+61"):
        return "0" + digits[3:]
    if digits.startswith("61") and len(digits) == 11:
        return "0" + digits[2:]
    return digits


def replyable(number):
    """Real phone numbers only: not short codes or named senders, which can't take a reply."""
    return bool(re.fullmatch(r"\+?\d{8,15}", number))


def masked(number):
    return number[:4] + "…" + number[-2:] if len(number) > 6 else "…"


def number_key(number):
    return hashlib.sha256(normalise_number(number).encode()).hexdigest()[:20]


class Store:
    """Reviewed conversations: one profile per number, plus a shared bank of the owner's texts."""

    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        os.makedirs(os.path.join(root, "profiles"), mode=0o700, exist_ok=True)

    def _read(self, path, default):
        try:
            with open(path) as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _write(self, path, data):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _profile_path(self, number):
        return os.path.join(self.root, "profiles", number_key(number) + ".json")

    def profile(self, number):
        return self._read(self._profile_path(number), None)

    def style(self):
        return self._read(os.path.join(self.root, "style.json"), {"notes": "", "examples": []})

    def save_review(self, number, name, summary, my_texts):
        with self.lock:
            self._write(self._profile_path(number), {
                "name": name, "summary": summary, "examples": my_texts[-60:],
                "reviewed": datetime.now().isoformat(timespec="seconds"),
            })
            style = self.style()
            key = number_key(number)
            known = {e["text"] for e in style["examples"]}
            added = [{"text": t, "key": key} for t in my_texts if t not in known]
            style["examples"] = (added[::-1] + style["examples"])[:EXAMPLES_KEPT]
            self._write(os.path.join(self.root, "style.json"), style)
            return len(added), style

    def save_style_notes(self, notes):
        with self.lock:
            style = self.style()
            style["notes"] = notes
            self._write(os.path.join(self.root, "style.json"), style)

    def forget(self, number):
        with self.lock:
            path = self._profile_path(number)
            existed = os.path.exists(path)
            if existed:
                os.remove(path)
            style = self.style()
            key = number_key(number)
            before = len(style["examples"])
            style["examples"] = [e for e in style["examples"] if e.get("key") != key]
            self._write(os.path.join(self.root, "style.json"), style)
            return existed, before - len(style["examples"])


# ==================== SORTING ==================== #

SMS_SYSTEM = triage.load_prompt("sms_system", """You sort incoming text messages (SMS) for a small local \
business. The owner's personal texts come to the same phone. Pick exactly one category and write a short \
summary. Answer with JSON only.""")

SMS_GUIDE = triage.CATEGORY_GUIDE + "\n\n" + triage.load_prompt("sms_categories", """These are text messages, \
not emails. Also spam: parcel, toll, bank, tax, prize or account messages from unknown numbers that ask you \
to click a link, log in or reply; "hi, is this ...?" wrong-number openers from strangers. marketing: \
promotions from shops, restaurants and services. personal: friends and family chatting, plans, anything not \
about the business.""")

# One-time codes must never be filtered, alerted or drafted for
CODE_RE = re.compile(
    r"(?i)\b(code|passcode|pin|otp|verification|verify|one[- ]time)\b.{0,40}?\b\d{4,8}\b"
    r"|\b\d{4,8}\b.{0,40}?\b(code|passcode|otp)\b", re.S)

SMS_TITLES = {
    "quote_request": "Quote request (text)",
    "customer_query": "Customer text",
    "business_admin": "Business admin (text)",
    "unclassified": "Text (not sorted)",
}


@dataclass
class SmsDecision:
    category: str
    reason: str
    filtered: bool = False
    topic: str = ""
    priority: int = 3
    summary: str = ""


def clip(text, limit):
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def thread_lines(thread, their_name, limit=THREAD_MESSAGES, chars=500):
    lines = []
    for m in thread[-limit:]:
        who = OWNER if m.get("from_me") else their_name
        lines.append(f"{who}: {clip(m.get('body'), chars)}")
    return "\n".join(lines)


def decide_sms(number, name, is_contact, body, thread, model=None):
    if CODE_RE.search(body):
        return SmsDecision("code", "one-time code")

    mail = triage.Mail(uid=0, account="sms", from_name=name or "", from_addr=number, to="", subject="",
                       body=clip(body, triage.BODY_CHARS))
    if not is_contact:
        rule = triage.spam_rule(mail)
        if rule:
            return SmsDecision("spam", f"spam rule: {rule}", filtered=True)

    who = f"{name} ({number})" if name else number
    text = f"Text message from {who}" + (", who is in the phone's contacts" if is_contact else ", not a contact")
    earlier = thread_lines([m for m in thread if m.get("body") != body][-4:], name or "Them", chars=300)
    if earlier:
        text += f"\n\nEarlier in this conversation:\n{earlier}"
    text += f"\n\nNew message:\n{mail.body or '(no text)'}"

    result = triage.classify(mail, model, system=SMS_SYSTEM, guide=SMS_GUIDE, text=text)
    if result is None:
        if is_contact:
            return SmsDecision("unclassified", "model unavailable")
        # Better a notification too many than a missed customer
        return SmsDecision("unclassified", "model unavailable", topic=triage.NTFY_MESSAGE_TOPIC, priority=3)
    category, summary = result

    if category in ("spam", "marketing") and not is_contact:
        return SmsDecision(category, "model", filtered=True, summary=summary)
    topic, priority = triage.TOPIC_FOR.get(category, ("", 3))
    if is_contact:
        priority = max(priority, 3)
    return SmsDecision(category, "contact" if is_contact else "model", topic=topic, priority=priority, summary=summary)


def notify_sms(number, name, decision):
    title = f"{SMS_TITLES.get(decision.category, 'Text')}: {name or number}"
    lines = [f"From: {name} {number}" if name else f"From: {number}"]
    if decision.summary:
        lines += ["", decision.summary]
    payload = {"topic": decision.topic, "title": title, "message": "\n".join(lines),
               "priority": decision.priority, "tags": ["sms"] + (["warning"] if decision.category == "unclassified" else [])}
    headers = {"Authorization": f"Bearer {triage.NTFY_TOKEN}"} if triage.NTFY_TOKEN else {}
    res = requests.post(triage.NTFY_URL + "/", json=payload, headers=headers, timeout=15)
    res.raise_for_status()


# ==================== WRITING ==================== #

DRAFT_SYSTEM = triage.load_prompt("draft_system", f"""You write text message reply drafts for {OWNER}, \
who runs a small local business. They read and edit every draft before sending it.

Put ONLY the text of {OWNER}'s next text message in "reply", in their voice: no reasoning, labels or \
explanations. Answer what they asked and stop: usually one or two short sentences, with no filler such \
as "let me know if you need anything else". Never invent prices, dates, times, addresses or promises; \
only if the reply can't make sense without one, leave a gap like [time] or [price] to fill in.""")

STYLE_FROM_EXAMPLES = f"Match the length, tone, spelling, punctuation and greetings of {OWNER}'s real texts above."
STYLE_DEFAULT = triage.load_prompt("draft_style_default", "Keep it short, friendly and casual.")

REVIEW_SYSTEM = triage.load_prompt("review_system", f"""You help {OWNER}, who runs a small local business, \
keep notes on the people they text. Plain sentences or bullet points inside the JSON field, no markdown headings.""")


def ollama_text(system, prompt, field, temperature=0.3, max_tokens=300):
    """The model's answer as one JSON string field. Without a fixed answer shape, qwen3 tends to
    think out loud in the reply even with thinking switched off; the schema makes it answer directly."""
    payload = {
        "model": triage.OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "format": {"type": "object", "properties": {field: {"type": "string"}}, "required": [field]},
        "stream": False,
        "keep_alive": int(triage.OLLAMA_KEEP_ALIVE) if triage.OLLAMA_KEEP_ALIVE.lstrip("-").isdigit()
                      else triage.OLLAMA_KEEP_ALIVE,
        "options": {"temperature": temperature, "num_ctx": triage.OLLAMA_NUM_CTX, "num_predict": max_tokens},
    }
    if triage.OLLAMA_THINK:
        payload["think"] = triage.OLLAMA_THINK.lower() == "true"
    res = requests.post(f"{triage.OLLAMA_URL}/api/chat", json=payload, timeout=triage.OLLAMA_TIMEOUT)
    res.raise_for_status()
    text = re.sub(r"(?s)<think>.*?</think>", "", res.json()["message"]["content"])
    return str(json.loads(text)[field]).strip()


def tidy_draft(text):
    text = re.sub(rf"^({re.escape(OWNER)}|Me|Reply)\s*:\s*", "", text.strip(), flags=re.I)
    return text.strip().strip('"“”').strip()


def pick_examples(store, number):
    """This person's texts first (how the owner talks to them), then the newest from everyone else."""
    profile = store.profile(number) or {}
    own = profile.get("examples", [])[-8:]
    others = [e["text"] for e in store.style()["examples"] if e.get("key") != number_key(number)]
    return own + others[:DRAFT_EXAMPLES - len(own)]


def write_draft(store, number, name, thread):
    if not thread:
        raise BadRequest("thread is empty")
    profile = store.profile(number) or {}
    style = store.style()
    them = name or profile.get("name") or "Them"
    parts = []
    if style.get("notes"):
        parts.append(f"How {OWNER} writes:\n{style['notes']}")
    examples = pick_examples(store, number)
    if examples:
        parts.append(f"Some of {OWNER}'s real texts:\n" + "\n".join(f"- {clip(t, 300)}" for t in examples))
    if profile.get("summary"):
        parts.append(f"Notes about {them}:\n{profile['summary']}")
    parts.append(f"Today is {datetime.now():%A %-d %B %Y}.")
    parts.append(f"Conversation with {them} (oldest first):\n{thread_lines(thread, them)}")
    parts.append(f"Write {OWNER}'s reply to {them}'s last message. "
                 + (STYLE_FROM_EXAMPLES if examples or style.get("notes") else STYLE_DEFAULT))
    return tidy_draft(ollama_text(DRAFT_SYSTEM, "\n\n".join(parts), "reply", temperature=0.5, max_tokens=250))


def review_conversation(store, number, name, messages):
    if not messages:
        raise BadRequest("conversation is empty")
    them = name or "Them"
    my_texts = [clip(m.get("body"), 400) for m in messages
                if m.get("from_me") and len(str(m.get("body") or "").strip()) >= 2]

    # The newest part of the conversation, sized to fit the model's context
    lines, used = [], 0
    for m in reversed(messages):
        line = f"{OWNER if m.get('from_me') else them}: {clip(m.get('body'), 400)}"
        if used + len(line) > 9000:
            break
        lines.append(line)
        used += len(line)
    convo = "\n".join(reversed(lines))

    summary = ollama_text(REVIEW_SYSTEM, (
        f"Here is {OWNER}'s text conversation with {them}, oldest first:\n\n{convo}\n\n"
        f"Write notes {OWNER} can use when replying to {them}, in at most 80 words: who they are to {OWNER}, "
        "any jobs (what, where, status, prices or times agreed), and how they like to communicate."
    ), "notes", max_tokens=200)
    added, style = store.save_review(number, name, summary, my_texts)

    sample = [e["text"] for e in style["examples"][:80]]
    random.shuffle(sample)
    notes = ""
    if len(sample) >= 5:
        notes = ollama_text(REVIEW_SYSTEM, (
            f"Here are text messages {OWNER} has sent:\n" + "\n".join(f"- {clip(t, 300)}" for t in sample[:60]) +
            f"\n\nDescribe how {OWNER} writes texts in at most 8 short bullet points: length, tone, greetings and "
            "sign-offs, slang, capitalisation and punctuation, emoji, and how they give prices and times."
        ), "style", max_tokens=250)
        store.save_style_notes(notes)
    return {"summary": summary, "style_notes": notes, "examples_added": added}


# ==================== HTTP ==================== #

def required(request, name, kind=str):
    value = request.get(name)
    if not isinstance(value, kind):
        raise BadRequest(f"missing {name}")
    return value


def messages_of(request, name):
    value = request.get(name, [])
    if not isinstance(value, list) or not all(isinstance(m, dict) for m in value):
        raise BadRequest(f"{name} must be a list of messages")
    return value


class App:
    def __init__(self):
        self.aead = load_key()
        self.guard = ReplayGuard()
        self.store = Store(STATE_DIR)
        self.routes = {
            "/sms/incoming": self.incoming,
            "/sms/draft": self.draft,
            "/sms/review": self.review,
            "/sms/forget": self.forget,
        }

    def incoming(self, req):
        number = normalise_number(required(req, "number"))
        name = str(req.get("name") or "").strip()
        is_contact = bool(req.get("is_contact"))
        body = required(req, "body")
        decision = decide_sms(number, name, is_contact, body, messages_of(req, "thread"))
        alerted = False
        if decision.topic:
            try:
                notify_sms(number, name, decision)
                alerted = True
            except Exception as err:
                log.error("ntfy failed: %s", err)
        log.info("incoming from %s -> %s (%s)%s", masked(number), decision.category, decision.reason,
                 ", filtered" if decision.filtered else "")
        return {
            "category": decision.category,
            "filtered": decision.filtered,
            "summary": decision.summary,
            "alerted": alerted,
            "draft": not decision.filtered and decision.category != "code" and replyable(number),
        }

    def draft(self, req):
        number = normalise_number(required(req, "number"))
        started = time.time()
        text = write_draft(self.store, number, str(req.get("name") or "").strip(), messages_of(req, "thread"))
        log.info("draft for %s in %.1fs", masked(number), time.time() - started)
        return {"draft": text}

    def review(self, req):
        number = normalise_number(required(req, "number"))
        result = review_conversation(self.store, number, str(req.get("name") or "").strip(),
                                     messages_of(req, "messages"))
        log.info("reviewed conversation with %s, %d new examples", masked(number), result["examples_added"])
        return result

    def forget(self, req):
        number = normalise_number(required(req, "number"))
        existed, removed = self.store.forget(number)
        log.info("forgot %s", masked(number))
        return {"profile_deleted": existed, "examples_removed": removed}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sms-triage"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass  # requests are logged by the endpoints, without their contents

        def send(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self.send(200, b"ok", "text/plain")
            else:
                self.send(404, b"")

        def do_POST(self):
            route = app.routes.get(self.path)
            if route is None:
                return self.send(404, b"")
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self.send(413, b"")
            try:
                req = unseal(app.aead, self.rfile.read(length), self.path)
                app.guard.check(req)
            except Refused:
                log.warning("refused a request to %s (wrong key, tampered, stale or replayed)", self.path)
                return self.send(401, b"")
            status = 200
            try:
                result = route(req)
            except BadRequest as err:
                status, result = 400, {"error": str(err)}
            except Exception as err:
                log.exception("%s failed", self.path)
                status, result = 502, {"error": f"{type(err).__name__}: server could not finish this"}
            # Bound to this request, so a response can't be swapped for another one
            self.send(status, seal(app.aead, result, f"{self.path}#{req['rid']}"))

    return Handler


def main():
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    app = App()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(app))
    log.info("sms-triage listening on %d with model %s", PORT, triage.OLLAMA_MODEL)
    log.info("Prompts from %s: %s", triage.PROMPTS_DIR, ", ".join(triage.PROMPTS_FROM_FILES) or "none, using built-in")
    server.serve_forever()


if __name__ == "__main__":
    main()
