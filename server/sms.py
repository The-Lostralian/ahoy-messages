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
    /sms/quick      pick a positive and a negative one-tap reply for the last message
    /sms/draft      write a full reply draft for a conversation (on request from the phone's menu)
    /sms/review     learn context and writing style from a whole conversation
    /sms/forget     delete what was learned from one number's conversation
    /sms/feedback   a suggested draft next to what the owner actually sent, to learn from
GET /health         plain "ok", for Uptime Kuma

Stored in /state/sms: reviewed conversations (notes and the owner's texts), corrections (drafts next to
what was really sent, with the few messages they answered), lessons.txt (what the owner keeps changing,
editable), and conversations queued for a deep review until it's done. Incoming texts and the recent
messages sent along for drafts are used and then forgotten. Message text is never logged.
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
DRAFT_EXAMPLES = 25            # examples shown to the model per draft
CORRECTIONS_KEPT = 2000        # draft vs what the owner actually sent, newest first
DRAFT_CORRECTIONS = 6          # corrections shown to the model per draft, the most similar first
LESSONS_EVERY = 10             # new corrections between updates of the lessons
PROMPT_CHARS = 11000           # rough cap on a draft prompt, to stay inside the model's context
REPLAY_CONTEXT = 8             # messages before each reply that a deep review drafts from
DEEP_HOURS = env("DEEP_REVIEW_HOURS", "")  # e.g. 22-7: when deep reviews may use the GPU; empty = any time
OWNER = env("OWNER_NAME", "Owner")  # the phone's owner, as the model sees them in conversations
SMS_TOPIC = env("NTFY_SMS_TOPIC", "ahoy-sms")  # every text from a contact, word for word; empty = off
NTFY_MAX_CHARS = 3500          # ntfy turns messages over 4096 bytes into attachments
LEARN_LESSONS = env("SMS_LEARN_LESSONS", "") == "true"  # off: qwen3:4b's lessons were the instructions echoed back


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

    # Corrections: what a draft said next to what the owner actually sent, from the phone (drafts the
    # owner edited or sent as they were) and from deep reviews (old replies drafted again)

    def corrections(self):
        return self._read(os.path.join(self.root, "corrections.json"), {"items": [], "since_lessons": 0})

    def add_correction(self, number, context, draft, sent, source):
        """context: the last few lines of the conversation before the reply. Returns the number of
        corrections since the lessons were last updated."""
        with self.lock:
            data = self.corrections()
            data["items"].insert(0, {"key": number_key(number), "context": context, "draft": draft, "sent": sent,
                                     "source": source, "at": datetime.now().isoformat(timespec="seconds")})
            del data["items"][CORRECTIONS_KEPT:]
            data["since_lessons"] += 1
            self._write(os.path.join(self.root, "corrections.json"), data)
            return data["since_lessons"]

    def lessons_done(self):
        with self.lock:
            data = self.corrections()
            data["since_lessons"] = 0
            self._write(os.path.join(self.root, "corrections.json"), data)

    # The lessons are plain text, so the owner can read and edit them; updates build on what's there
    def lessons(self):
        try:
            with open(os.path.join(self.root, "lessons.txt"), encoding="utf-8") as fh:
                return fh.read().strip()
        except FileNotFoundError:
            return ""

    def save_lessons(self, text):
        with self.lock:
            path = os.path.join(self.root, "lessons.txt")
            fd, tmp = tempfile.mkstemp(dir=self.root)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text.strip() + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)

    # Deep review jobs: one file per number, holding the conversation and the replies still to do

    def _job_path(self, number):
        return os.path.join(self.root, "jobs", number_key(number) + ".json")

    def save_job(self, number, job):
        os.makedirs(os.path.join(self.root, "jobs"), mode=0o700, exist_ok=True)
        with self.lock:
            self._write(self._job_path(number), job)

    def jobs(self):
        folder = os.path.join(self.root, "jobs")
        paths = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        return [job for job in (self._read(os.path.join(folder, p), None) for p in paths) if job]

    def drop_job(self, number):
        with self.lock:
            try:
                os.remove(self._job_path(number))
                return True
            except FileNotFoundError:
                return False

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
            data = self.corrections()
            data["items"] = [c for c in data["items"] if c.get("key") != key]
            self._write(os.path.join(self.root, "corrections.json"), data)
        self.drop_job(number)
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
    title: str = ""
    full_text: bool = False   # the notification carries the whole text rather than the summary


# ==================== PEOPLE ==================== #

@dataclass
class Person:
    rules: list   # (priority, title, pattern)
    notes: list   # lines for the model, when sorting their texts and drafting replies


def person_key(who):
    who = str(who or "").strip()
    return who.casefold() if re.search(r"[A-Za-z]", who) else normalise_number(who)


def load_people(text):
    """people.txt: a [contact name or number] heading per person, then any number of
        alert <priority 1-5> <title> | <pattern>
    lines (a text from them matching the pattern, case-insensitive, is sent to ntfy as an alert)
    and plain lines of notes about them."""
    people, current = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        heading = re.fullmatch(r"\[(.+)\]", line)
        if heading:
            current = people.setdefault(person_key(heading.group(1)), Person([], []))
        elif current is None:
            log.warning("people.txt: %r is not under a [name] heading, ignored", line[:40])
        elif alert := re.fullmatch(r"(?i)alert\s+([1-5])\s+(.+?)\s*\|\s*(.+)", line):
            try:
                current.rules.append((int(alert.group(1)), alert.group(2), re.compile(alert.group(3), re.I | re.S)))
            except re.error as err:
                log.warning("people.txt: bad pattern for %r: %s", alert.group(2), err)
        else:
            current.notes.append(line)
    return people


PEOPLE = load_people(triage.load_prompt("people", ""))


def find_person(number, name):
    return PEOPLE.get(normalise_number(number)) or (PEOPLE.get(person_key(name)) if name else None)


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

    person = find_person(number, name if is_contact else "")
    for priority, title, pattern in person.rules if person else []:
        if pattern.search(body):
            # The owner chose these texts, so the alert carries the text itself rather than a summary
            return SmsDecision("alert", f"rule: {title}", topic=triage.NTFY_MESSAGE_TOPIC, priority=priority,
                               summary=clip(body, 300), title=title)

    mail = triage.Mail(uid=0, account="sms", from_name=name or "", from_addr=number, to="", subject="",
                       body=clip(body, triage.BODY_CHARS))
    if not is_contact:
        rule = triage.spam_rule(mail)
        if rule:
            return SmsDecision("spam", f"spam rule: {rule}", filtered=True)

    who = f"{name} ({number})" if name else number
    text = f"Text message from {who}" + (", who is in the phone's contacts" if is_contact else ", not a contact")
    if person and person.notes:
        text += f"\n\n{OWNER}'s notes about them:\n" + "\n".join(person.notes)
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


def forward_contact_text(decision, is_contact):
    """Texts from contacts always reach ntfy (and so the desk Kindle) word for word: ones that alert
    anyway carry the whole text instead of a summary, and the rest go to SMS_TOPIC. One-time codes never."""
    if not is_contact or decision.category == "code" or decision.filtered or not SMS_TOPIC:
        return decision
    decision.full_text = True
    if not decision.topic:
        decision.topic, decision.priority = SMS_TOPIC, 3
    return decision


def notify_sms(number, name, decision, sent_at=None, body=""):
    text = clip(body, NTFY_MAX_CHARS) if decision.full_text else decision.summary
    if decision.full_text and decision.topic == SMS_TOPIC:
        # A plain forward: the sender as the title and their text as the message, nothing else
        title, lines = name or number, []
    else:
        title = f"{decision.title or SMS_TITLES.get(decision.category, 'Text')}: {name or number}"
        lines = [f"From: {name} {number}" if name else f"From: {number}"]
    if sent_at and time.time() - sent_at > 300:
        lines.append(f"Sent {datetime.fromtimestamp(sent_at):%-I:%M %p} (the phone couldn't reach the server then)")
    if text:
        lines += ([""] if lines else []) + [text]
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


class Interrupted(Exception):
    """Background work stopped so a text could have the model."""


def ollama_text(system, prompt, field, temperature=0.3, max_tokens=300, interrupt=None):
    """The model's answer as one JSON string field. Without a fixed answer shape, qwen3 tends to
    think out loud in the reply even with thinking switched off; the schema makes it answer directly."""
    schema = {"type": "object", "properties": {field: {"type": "string"}}, "required": [field]}
    answer = ollama_json(system, prompt, schema, temperature, max_tokens, interrupt)
    return str(answer[field]).strip()


def ollama_json(system, prompt, schema, temperature=0.3, max_tokens=300, interrupt=None):
    """The model's answer as a dict shaped by the JSON schema (Ollama enforces it while generating,
    so an "enum" field can only ever be one of its values).
    With interrupt (background work), the answer is streamed and abandoned as soon as interrupt()
    is true; closing the connection makes Ollama stop generating, freeing the GPU."""
    payload = {
        "model": triage.OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "format": schema,
        "stream": False,
        "keep_alive": int(triage.OLLAMA_KEEP_ALIVE) if triage.OLLAMA_KEEP_ALIVE.lstrip("-").isdigit()
                      else triage.OLLAMA_KEEP_ALIVE,
        "options": {"temperature": temperature, "num_ctx": triage.OLLAMA_NUM_CTX, "num_predict": max_tokens},
    }
    if triage.OLLAMA_THINK:
        payload["think"] = triage.OLLAMA_THINK.lower() == "true"
    if interrupt is None:
        res = requests.post(f"{triage.OLLAMA_URL}/api/chat", json=payload, timeout=triage.OLLAMA_TIMEOUT)
        res.raise_for_status()
        content = res.json()["message"]["content"]
    else:
        if interrupt():
            raise Interrupted()
        payload["stream"] = True
        parts = []
        with requests.post(f"{triage.OLLAMA_URL}/api/chat", json=payload, timeout=triage.OLLAMA_TIMEOUT,
                           stream=True) as res:
            res.raise_for_status()
            for line in res.iter_lines(chunk_size=64):
                if interrupt():
                    raise Interrupted()
                if line:
                    parts.append(json.loads(line).get("message", {}).get("content", ""))
        content = "".join(parts)
    text = re.sub(r"(?s)<think>.*?</think>", "", content)
    return json.loads(text)


def tidy_draft(text):
    text = re.sub(rf"^({re.escape(OWNER)}|Me|Reply)\s*:\s*", "", text.strip(), flags=re.I)
    return text.strip().strip('"“”').strip()


def pick_examples(store, number, exclude=()):
    """This person's texts first (how the owner talks to them), then the newest from everyone else."""
    profile = store.profile(number) or {}
    # Texts to other people are left out: the model copied what they said (a shopping order sent to one
    # person turned up in replies to another), not just how they were written
    return [t for t in profile.get("examples", []) if t not in exclude][-DRAFT_EXAMPLES:]


def same_text(a, b):
    squash = lambda t: re.sub(r"[^a-z0-9]+", " ", str(t).casefold()).strip()
    return squash(a) == squash(b)


def words(text):
    return set(re.findall(r"[a-z']{3,}", str(text).casefold()))


def pick_corrections(store, number, thread, exclude=()):
    """Drafts the owner changed, most like the message being answered first, this person's first on
    a tie. Drafts sent as they were teach nothing about what to change, so they're left out."""
    last = next((m.get("body") for m in reversed(thread) if not m.get("from_me")), "")
    target, key = words(last), number_key(number)
    scored = []
    for i, c in enumerate(store.corrections()["items"]):
        if same_text(c["draft"], c["sent"]) or c["sent"] in exclude or not c.get("context") or c.get("key") != key:
            continue
        overlap = len(target & words(c["context"][-1]))
        scored.append((overlap + (1 if c.get("key") == key else 0), -i, c))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [c for _, _, c in scored[:DRAFT_CORRECTIONS]]


def write_draft(store, number, name, thread, exclude=(), interrupt=None, dated=True):
    """exclude: texts the model mustn't see as examples (a deep review's real answer)."""
    if not thread:
        raise BadRequest("thread is empty")
    profile = store.profile(number) or {}
    style = store.style()
    them = name or profile.get("name") or "Them"
    examples = pick_examples(store, number, exclude)
    corrections = pick_corrections(store, number, thread, exclude)
    lessons = store.lessons()

    def build():
        parts = []
        # The style notes and lessons qwen3:4b wrote were its instructions echoed back, so they're only
        # used once someone turns lessons back on (and has checked lessons.txt and style.json)
        if LEARN_LESSONS and style.get("notes"):
            parts.append(f"How {OWNER} writes:\n{style['notes']}")
        if LEARN_LESSONS and lessons:
            parts.append(f"What {OWNER} changes in drafts (follow these):\n{lessons}")
        if examples:
            parts.append(f"Some of {OWNER}'s real texts:\n" + "\n".join(f"- {clip(t, 200)}" for t in examples))
        if corrections:
            parts.append(f"Earlier drafts {OWNER} changed before sending:\n" + "\n".join(
                f"- To \"{clip(c['context'][-1], 150)}\" the draft was \"{clip(c['draft'], 200)}\"; "
                f"{OWNER} sent \"{clip(c['sent'], 200)}\"" for c in corrections))
        if profile.get("summary"):
            parts.append(f"Notes about {them}:\n{profile['summary']}")
        person = find_person(number, name)
        if person and person.notes:
            parts.append(f"{OWNER}'s own notes about {them}:\n" + "\n".join(person.notes))
        if dated:
            parts.append(f"Today is {datetime.now():%A %-d %B %Y}.")
        parts.append(f"Conversation with {them} (oldest first):\n{thread_lines(thread, them, chars=300)}")
        parts.append(f"Write {OWNER}'s reply to {them}'s last message. "
                     + (STYLE_FROM_EXAMPLES if examples else STYLE_DEFAULT))
        return "\n\n".join(parts)

    prompt = build()
    while len(prompt) > PROMPT_CHARS and (len(examples) > 8 or corrections):
        if len(examples) > 8:
            examples.pop()
        else:
            corrections.pop()
        prompt = build()
    return tidy_draft(ollama_text(DRAFT_SYSTEM, prompt, "reply", temperature=0.5, max_tokens=250,
                                  interrupt=interrupt))


def update_lessons(store, interrupt=None):
    """Asks the model what the owner keeps changing in drafts, building on the current lessons."""
    changed = [c for c in store.corrections()["items"] if not same_text(c["draft"], c["sent"])][:30]
    if len(changed) < 5:
        return False
    current = store.lessons()
    pairs = "\n".join(f"- To \"{clip(c['context'][-1], 120)}\": draft \"{clip(c['draft'], 160)}\" / "
                      f"{OWNER} sent \"{clip(c['sent'], 160)}\"" for c in changed)
    lessons = ollama_text(REVIEW_SYSTEM, (
        (f"The current lessons are:\n{current}\n\n" if current else "") +
        f"Here are reply drafts and what {OWNER} actually sent instead, newest first:\n{pairs}\n\n"
        f"Write at most 10 short bullet points (each starting with \"- \") on what {OWNER} consistently changes: "
        "length, tone, greetings, words and phrases, punctuation, what to leave out or add. Keep current lessons "
        "that still hold, fix any these drafts contradict, and leave out one-off facts about a particular job."
    ), "lessons", max_tokens=350, interrupt=interrupt)
    store.save_lessons(lessons)
    store.lessons_done()
    log.info("lessons updated from %d changed drafts", len(changed))
    return True


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


# ==================== QUICK REPLIES ==================== #

# Most texts only need "Yep" or "No thanks". The model only says what type of message the other
# person's last text is (sorting is something a small model does well; writing replies from scratch
# made it copy messages and make things up), and each type has fixed replies in quick_replies.txt.
# The phone shows them as two buttons, positive first.

QUICK_DEFAULT = """[offer] they offer something, e.g. "Want a cuppa?"
Yes pls | No thanks
[favour] they ask for a favour, e.g. "Can you call me?"
Yes, no worries | Can't right now
[yes_no] a question yes or no answers, e.g. "Are you home?"
Yep | Nope
[news] news or an update that needs no answer
Ok
[thanks] they say thanks
Thanks
[open] a question yes or no can't answer: what, when, where, how much"""

QUICK_INTRO = triage.load_prompt("quick_system", f"""You sort text messages {OWNER} receives by what kind of \
reply they need. Answer with JSON only.""")


@dataclass
class QuickType:
    description: str
    replies: list  # positive first; empty for types that get no buttons


def load_quick_types(text):
    """quick_replies.txt: "[type] description" headings, each followed by at most one
    "positive | negative" line; # lines are comments."""
    types, current = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        heading = re.fullmatch(r"\[([\w-]+)\]\s*(.*)", line)
        if heading:
            current = types.setdefault(heading.group(1), QuickType(heading.group(2).strip(), []))
        elif current is None:
            log.warning("quick_replies.txt: %r is not under a [type] heading, ignored", line[:40])
        elif current.replies:
            log.warning("quick_replies.txt: more than one reply line under a type, %r ignored", line[:40])
        else:
            current.replies = list(dict.fromkeys(r.strip() for r in line.split("|") if r.strip()))[:2]
    if len(types) < 2:
        log.warning("quick_replies.txt has fewer than two types; using the built-in ones")
        return load_quick_types(QUICK_DEFAULT)
    return types


QUICK_TYPES = load_quick_types(triage.load_prompt("quick_replies", QUICK_DEFAULT))
QUICK_SYSTEM = QUICK_INTRO + "\n\nTypes:\n" + "\n".join(
    f"- {name}: {t.description}" for name, t in QUICK_TYPES.items())


def quick_replies(number, name, thread):
    """(type, replies) for the other person's last message. No replies when the owner has already
    answered it, or for a type with none (an open question)."""
    thread = [m for m in thread if str(m.get("body") or "").strip()]
    if not thread or thread[-1].get("from_me"):
        return "answered", []
    them = name or "Them"
    prompt = (f"Conversation with {them}, oldest first:\n{thread_lines(thread[-6:], them, chars=300)}\n\n"
              f"What type is {them}'s last message?")
    schema = {"type": "object", "properties": {"type": {"type": "string", "enum": list(QUICK_TYPES)}},
              "required": ["type"]}
    kind = ollama_json(QUICK_SYSTEM, prompt, schema, temperature=0, max_tokens=20).get("type")
    if kind not in QUICK_TYPES:
        return "unknown", []
    return kind, QUICK_TYPES[kind].replies


# ==================== DEEP REVIEW ==================== #

def in_hours(spec, hour):
    """'22-7' is 10 pm to 7 am, '9-17' is 9 am to 5 pm, empty is always."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", spec or "")
    if not m:
        return True
    start, end = int(m.group(1)) % 24, int(m.group(2)) % 24
    return start <= hour < end if start < end else hour >= start or hour < end


def reply_starts(messages):
    """Index of the first message of each run of the owner's messages that answers someone."""
    return [i for i, m in enumerate(messages)
            if m.get("from_me") and i > 0 and not messages[i - 1].get("from_me")]


class Live:
    """Counts requests from the phone in progress. Background work waits until the phone has been quiet
    for a little while, since a new text is usually followed by a draft request."""

    QUIET_SECONDS = 20

    def __init__(self):
        self.active = 0
        self.last = 0.0
        self.lock = threading.Lock()

    def __enter__(self):
        with self.lock:
            self.active += 1
            self.last = time.time()

    def __exit__(self, *exc):
        with self.lock:
            self.active -= 1
            self.last = time.time()

    def busy(self):
        return self.active > 0 or time.time() - self.last < self.QUIET_SECONDS


class DeepReviewer(threading.Thread):
    """Drafts each of the owner's old replies from the messages before it, as if it were new, and keeps
    the draft next to what the owner really sent. One reply at a time, newest first, only in DEEP_HOURS,
    giving way to the phone; progress is saved after each reply, so restarts lose nothing."""

    def __init__(self, store, live):
        super().__init__(daemon=True, name="deep-review")
        self.store, self.live = store, live
        self.wake = threading.Event()
        self.lessons_due = False

    def run(self):
        while True:
            try:
                if self.live.busy():
                    time.sleep(2)
                elif self.lessons_due:
                    update_lessons(self.store, interrupt=self.live.busy)
                    self.lessons_due = False
                elif not self.step():
                    self.wake.wait(60)
                    self.wake.clear()
            except Interrupted:
                log.info("deep review paused for a request from the phone")  # resumes once it's quiet
            except Exception:
                log.exception("deep review step failed; trying again in a minute")
                time.sleep(60)

    def step(self):
        """Does one reply of the oldest queued job. False if there was nothing to do now."""
        if not in_hours(DEEP_HOURS, datetime.now().hour):
            return False
        jobs = self.store.jobs()
        if not jobs:
            return False
        job = jobs[0]
        number, messages = job["number"], job["messages"]
        if not job["todo"]:
            self.store.drop_job(number)
            return True
        start = job["todo"][0]
        end = start
        while end < len(messages) and messages[end].get("from_me"):
            end += 1
        sent = "\n".join(str(m.get("body") or "").strip() for m in messages[start:end]).strip()
        context = messages[max(0, start - REPLAY_CONTEXT):start]
        if sent and context:
            exclude = {str(m.get("body") or "").strip() for m in messages[start:end]} | {sent}
            draft = write_draft(self.store, number, job.get("name", ""), context, exclude=exclude,
                                interrupt=self.live.busy, dated=False)
            lines = [str(m.get("body") or "") for m in context if not m.get("from_me")][-3:]
            if self.store.add_correction(number, lines, draft, sent, "deep review") >= LESSONS_EVERY and LEARN_LESSONS:
                self.lessons_due = True
        if not os.path.exists(self.store._job_path(number)):
            return True  # forgotten meanwhile
        job["todo"].pop(0)
        job["done"] += 1
        if job["todo"]:
            self.store.save_job(number, job)
            if job["done"] % 25 == 0:
                log.info("deep review of %s: %d of %d replies", masked(number), job["done"], job["total"])
        else:
            self.store.drop_job(number)
            self.lessons_due = LEARN_LESSONS
            log.info("deep review of %s finished: %d replies", masked(number), job["total"])
        return True

    def queue(self, number, name, messages):
        todo = reply_starts(messages)[::-1]  # newest first: the way the owner writes now matters most
        self.store.save_job(number, {"number": number, "name": name, "messages": messages,
                                     "todo": todo, "done": 0, "total": len(todo)})
        self.wake.set()
        log.info("deep review of %s queued: %d replies%s", masked(number), len(todo),
                 f", runs {DEEP_HOURS}" if DEEP_HOURS else "")
        return len(todo)


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


class Recent:
    """Answers to the last hour's texts. When the phone gives up waiting for an answer it sends the text
    again later; that retry gets the first answer, so the text isn't alerted twice."""

    def __init__(self):
        self.entries = {}
        self.lock = threading.Lock()

    def claim(self, key):
        """(entry, first): first is True if this is the first time the text has been seen."""
        with self.lock:
            cutoff = time.time() - 3600
            self.entries = {k: e for k, e in self.entries.items() if e["at"] > cutoff}
            entry = self.entries.get(key)
            if entry:
                return entry, False
            entry = self.entries[key] = {"at": time.time(), "done": threading.Event(), "result": None}
            return entry, True


class App:
    def __init__(self):
        self.aead = load_key()
        self.guard = ReplayGuard()
        self.store = Store(STATE_DIR)
        self.recent = Recent()
        self.live = Live()
        self.reviewer = DeepReviewer(self.store, self.live)
        self.routes = {
            "/sms/incoming": self.incoming,
            "/sms/draft": self.draft,
            "/sms/quick": self.quick,
            "/sms/review": self.review,
            "/sms/forget": self.forget,
            "/sms/feedback": self.feedback,
        }

    def incoming(self, req):
        number = normalise_number(required(req, "number"))
        body = required(req, "body")
        sent_at = req.get("sent_at")
        sent_at = sent_at / 1000 if isinstance(sent_at, (int, float)) and sent_at > 0 else None  # phone sends ms
        late = " (retried by the phone)" if req.get("late") else ""
        if sent_at is None:
            return self.sort_incoming(req, number, body, None, late)
        key = hashlib.sha256(f"{number}|{sent_at}|{body}".encode()).hexdigest()
        entry, first = self.recent.claim(key)
        if not first:
            entry["done"].wait(120)  # the first request may still be with the model
            if entry["result"] is not None:
                log.info("incoming from %s again%s, already sorted: %s", masked(number), late, entry["result"]["category"])
                return entry["result"]
        try:
            entry["result"] = self.sort_incoming(req, number, body, sent_at, late)
            return entry["result"]
        finally:
            entry["done"].set()

    def sort_incoming(self, req, number, body, sent_at, late):
        name = str(req.get("name") or "").strip()
        is_contact = bool(req.get("is_contact"))
        started = time.time()
        decision = forward_contact_text(decide_sms(number, name, is_contact, body, messages_of(req, "thread")),
                                        is_contact)
        alerted = False
        if decision.topic:
            try:
                notify_sms(number, name, decision, sent_at, body)
                alerted = True
            except Exception as err:
                log.error("ntfy failed: %s", err)
        log.info("incoming from %s -> %s (%s)%s in %.1fs%s", masked(number), decision.category, decision.reason,
                 ", filtered" if decision.filtered else "", time.time() - started, late)
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

    def quick(self, req):
        number = normalise_number(required(req, "number"))
        started = time.time()
        kind, replies = quick_replies(number, str(req.get("name") or "").strip(), messages_of(req, "thread"))
        log.info("quick replies for %s: %s, %d in %.1fs", masked(number), kind, len(replies), time.time() - started)
        return {"kind": kind, "replies": replies}

    def review(self, req):
        number = normalise_number(required(req, "number"))
        name = str(req.get("name") or "").strip()
        messages = messages_of(req, "messages")
        result = review_conversation(self.store, number, name, messages)
        log.info("reviewed conversation with %s, %d new examples", masked(number), result["examples_added"])
        if req.get("deep"):
            result["deep_queued"] = self.reviewer.queue(number, name, messages)
            result["deep_hours"] = DEEP_HOURS
        return result

    def feedback(self, req):
        """A draft the phone suggested, and what the owner actually sent."""
        number = normalise_number(required(req, "number"))
        draft, sent = required(req, "draft").strip(), required(req, "sent").strip()
        context = [str(m.get("body") or "") for m in messages_of(req, "context") if not m.get("from_me")][-3:]
        if not draft or not sent or not context:
            raise BadRequest("draft, sent and context are needed")
        same = same_text(draft, sent)
        if self.store.add_correction(number, context, draft, sent, "phone") >= LESSONS_EVERY and LEARN_LESSONS:
            self.reviewer.lessons_due = True
        log.info("feedback for %s: draft %s", masked(number), "sent as it was" if same else "changed")
        return {"stored": True, "same": same}

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
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                log.info("%s: the phone stopped waiting before the answer was ready", self.path)

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
                with app.live:
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
    app.reviewer.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(app))
    log.info("sms-triage listening on %d with model %s", PORT, triage.OLLAMA_MODEL)
    log.info("Prompts from %s: %s", triage.PROMPTS_DIR, ", ".join(triage.PROMPTS_FROM_FILES) or "none, using built-in")
    queued = sum(len(j["todo"]) for j in app.store.jobs())
    log.info("deep reviews run %s; %d replies queued", f"from {DEEP_HOURS}" if DEEP_HOURS else "any time", queued)
    log.info("quick replies: %d types; contact texts to ntfy topic %s", len(QUICK_TYPES), SMS_TOPIC or "(off)")
    if PEOPLE:
        log.info("people.txt: %d people, %d alert rules", len(PEOPLE), sum(len(p.rules) for p in PEOPLE.values()))
    server.serve_forever()


if __name__ == "__main__":
    main()
