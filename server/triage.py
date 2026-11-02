"""
mail-triage: watches the business inboxes over IMAP (e.g. through Proton Bridge) and pushes the emails that
matter (quote requests, customer messages, real business admin) to ntfy.

Order of decisions for each new email:
  1. From one of our own addresses           -> ignore (website confirmations, summaries)
  2. Sender is in the business address book  -> notify
  3. Reply to something we sent              -> notify
  4. Spam rules (takeoffs, SEO, freelance...) -> ignore
  5. "quote request" in the subject          -> notify as a real-estate quote request
  6. Everything else                         -> the local model picks a category

Mail is only ever read with BODY.PEEK on a read-only mailbox, so nothing is marked as read.
Progress is tracked by IMAP UID in /state/state.json; on first start it begins from the newest
message instead of notifying for the whole inbox.
"""

import email
import email.policy
import html
import imaplib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from email.utils import getaddresses, parseaddr

import requests

log = logging.getLogger("mail-triage")


def env(name, default=None):
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def env_list(name, default=""):
    return [v.strip().lower() for v in env(name, default).split(",") if v.strip()]


IMAP_HOST = env("IMAP_HOST", "protonmail-bridge")
IMAP_PORT = int(env("IMAP_PORT", "143"))
# "" plain (Proton Bridge on a private Docker network), "ssl" (usually port 993) or "starttls" (port 143)
IMAP_TLS = env("IMAP_TLS", "").lower()
ACCOUNTS = env_list("IMAP_ACCOUNTS", "main")
POLL_SECONDS = int(env("POLL_SECONDS", "60"))
BATCH_LIMIT = int(env("BATCH_LIMIT", "20"))
SENT_FOLDER = env("SENT_FOLDER", "Sent")
SENT_DAYS = int(env("SENT_DAYS", "120"))

RADICALE_URL = env("RADICALE_URL", "http://radicale:5232").rstrip("/")
RADICALE_USER = env("RADICALE_USER", "")
RADICALE_PASS = env("RADICALE_PASS", "")
CONTACT_BOOKS = [p for p in env("CONTACT_BOOKS", "").split(",") if p.strip()]
CONTACTS_REFRESH_SECONDS = int(env("CONTACTS_REFRESH_SECONDS", "900"))

OLLAMA_URL = env("OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = env("OLLAMA_MODEL", "qwen2.5:3b")
# Only for models that "think" (e.g. qwen3): set OLLAMA_THINK=false to switch it off
OLLAMA_THINK = env("OLLAMA_THINK", "")
OLLAMA_NUM_CTX = int(env("OLLAMA_NUM_CTX", "2048"))
# -1 keeps the model loaded on the T1000 between emails
OLLAMA_KEEP_ALIVE = env("OLLAMA_KEEP_ALIVE", "-1")
OLLAMA_TIMEOUT = int(env("OLLAMA_TIMEOUT", "180"))
BODY_CHARS = int(env("BODY_CHARS", "1500"))

NTFY_URL = env("NTFY_URL", "http://ntfy").rstrip("/")
NTFY_TOKEN = env("NTFY_TOKEN", "")
NTFY_QUOTE_TOPIC = env("NTFY_QUOTE_TOPIC", "ahoy-quote")
NTFY_MESSAGE_TOPIC = env("NTFY_MESSAGE_TOPIC", "ahoy-webmsg")
KUMA_PUSH_URL = env("KUMA_PUSH_URL", "")

OWN_DOMAINS = env_list("OWN_DOMAINS", "")
OWN_ADDRESSES = env_list("OWN_ADDRESSES", "")

STATE_FILE = env("STATE_FILE", "/state/state.json")

# Your own prompts: <name>.txt files in this folder replace the generic built-in ones (see prompts/)
PROMPTS_DIR = env("PROMPTS_DIR", "/prompts")
PROMPTS_FROM_FILES = []


def load_prompt(name, default):
    """PROMPTS_DIR/<name>.txt if it exists and isn't empty, otherwise the built-in default."""
    try:
        with open(os.path.join(PROMPTS_DIR, name + ".txt"), encoding="utf-8") as f:
            text = f.read().strip()
    except FileNotFoundError:
        return default
    if not text:
        return default
    PROMPTS_FROM_FILES.append(name)
    return text


# ==================== EMAIL PARSING ==================== #

@dataclass
class Mail:
    uid: int
    account: str
    from_name: str
    from_addr: str
    to: str
    subject: str
    body: str
    attachments: list = field(default_factory=list)
    reply_to_ids: set = field(default_factory=set)
    bulk: bool = False

    @property
    def domain(self):
        return self.from_addr.rpartition("@")[2]

    @property
    def text(self):
        return f"{self.subject}\n{self.body}"


def html_to_text(markup):
    markup = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", markup)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def clean_body(text):
    lines = []
    for line in text.splitlines():
        # Stop at the quoted conversation in a reply
        if re.match(r"^\s*On .{5,200} wrote:\s*$", line) or re.match(r"^-{2,}\s*Original Message", line, re.I):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line.rstrip())
    text = "\n".join(lines)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def message_ids(value):
    return set(re.findall(r"<[^<>\s]+>", value or ""))


def parse_mail(raw, uid=0, account=""):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    from_name, from_addr = parseaddr(str(msg.get("From", "")))
    to = ", ".join(addr for _, addr in getaddresses([str(msg.get("To", "")), str(msg.get("Delivered-To", ""))]) if addr)

    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            content = part.get_payload(decode=True).decode("utf-8", errors="replace")
        body = html_to_text(content) if part.get_content_type() == "text/html" else content

    attachments = [a.get_filename() for a in msg.iter_attachments() if a.get_filename()]
    bulk = bool(msg.get("List-Unsubscribe")) or str(msg.get("Precedence", "")).lower() in ("bulk", "list")

    return Mail(
        uid=uid,
        account=account,
        from_name=from_name.strip(),
        from_addr=from_addr.strip().lower(),
        to=to.lower(),
        subject=str(msg.get("Subject", "")).strip(),
        body=clean_body(body)[:BODY_CHARS],
        attachments=attachments,
        reply_to_ids=message_ids(str(msg.get("In-Reply-To", ""))) | message_ids(str(msg.get("References", ""))),
        bulk=bulk,
    )


# ==================== RULES ==================== #

def from_domain(mail, *domains):
    return any(mail.domain == d or mail.domain.endswith("." + d) for d in domains)


COUNTRIES = (
    "United States|USA|U\\.S\\.A?\\.?|America|Canada(?! Bay)|United Kingdom|UK|(?<!New )England|Scotland|Ireland|India|Pakistan|"
    "Bangladesh|Sri Lanka|Nepal|Philippines|China|Hong Kong|Vietnam|Indonesia|Malaysia|Singapore|Thailand|"
    "Nigeria|Ghana|Kenya|South Africa|Egypt|Dubai|UAE|United Arab Emirates|Saudi Arabia|Qatar|Germany|France|"
    "Netherlands|Spain|Italy|Poland|Ukraine|Russia|Turkey|Mexico|Brazil|Argentina|Colombia"
)
# Capitalised names only, so "china cabinet" or "turkey" in a recipe don't count
COUNTRY_RE = re.compile(rf"\b(?:the )?({COUNTRIES})\b")
# "when we get back from the UK", "arriving from India"...
TRAVEL_RE = re.compile(
    r"(?i)\b(get(ting)? back|back|return(s|ing|ed)?|arriv(e|es|ing|ed|al)|fly(ing)?( home| back| in)?|"
    r"flight|coming home|home|visiting|holiday(ing)?|travel(l?ing)?)\s+(from|in|to)\s+(the\s+)?$"
)


def mentions_overseas(text):
    """True if a country other than Australia is mentioned, other than as somewhere the sender is travelling."""
    for match in COUNTRY_RE.finditer(text):
        before = text[max(0, match.start() - 60):match.start()]
        if not TRAVEL_RE.search(before):
            return True
    return False


SPAM_RULES = [
    ("takeoffs", lambda m: re.search(r"(?i)\btake-?offs?\b", m.text)),
    ("estimating", lambda m: re.search(r"(?i)\bestimating\b", m.text)),
    ("seo", lambda m: re.search(r"(?i)\bSEO\b", m.text) and not from_domain(m, "google.com")),
    # Company / business-name registration or renewal, within a few words of each other
    ("registration", lambda m: re.search(
        r"(?i)\b(company|business name|abn|acn|asic)\b.{0,40}\b(registration|renewal|renew)\b"
        r"|\b(registration|renewal|renew)\b.{0,40}\b(company|business name|abn|acn|asic)\b", m.text, re.S)
        and not from_domain(m, "asic.gov.au")),
    ("supplier", lambda m: re.search(
        r"(?i)\b(become|becoming|be)\s+(a|an|our|your)\s+(\w+\s+)?(supplier|vendor|distributor)\b"
        r"|\b(supplier|vendor)\s+(registration|onboarding|application|partnership)\b", m.text)),
    ("shipping", lambda m: re.search(
        r"(?i)\b(products|goods|cargo)\b.{0,60}\bship(ping|ment)\b"
        r"|\bship(ping|ment)\b.{0,60}\b(products|goods|cargo|freight rates)\b", m.text, re.S)),
    ("freelance", lambda m: re.search(r"(?i)\bfreelanc", m.text)),
    ("overseas", lambda m: mentions_overseas(m.text)),
]


def spam_rule(mail):
    for name, test in SPAM_RULES:
        if test(mail):
            return name
    return None


# ==================== MODEL ==================== #

CATEGORIES = ["quote_request", "customer_query", "business_admin", "marketing", "spam", "personal"]

# Generic prompts for a small local business. The model's answer is held to CATEGORIES and SCHEMA by
# the code, so a replacement categories.txt must describe these same six category names.
SYSTEM_PROMPT = load_prompt("email_system", """You sort incoming email for a small local business. \
Pick exactly one category and write a short summary. Answer with JSON only.""")

CATEGORY_GUIDE = load_prompt("categories", """Categories:
- quote_request: NEW work. Someone describes a job and wants a price, a quote, a rate, someone to come and look, \
or to know if you're available. Use this whenever a job is described and cost or availability is asked, \
even if it is phrased as a question.
- customer_query: EXISTING work. A customer writing about a job that is already booked, done or underway: \
confirming or changing a time, access details, photos, a complaint, or paying or asking about an invoice \
the business sent them.
- business_admin: organisations (not customers) the business must deal with: tax office, insurance, bank, \
licensing, invoices or receipts for things the business bought.
- marketing: newsletters, promotions, sales pitches, lead-generation or advertising offers, platform notifications \
with nothing to act on.
- spam: scams, phishing, offers of outsourced services, anything unrelated to the business.
- personal: private mail that isn't about the business.

summary: at most 15 words saying what the sender wants, e.g. "Fix drooping curtain rod brackets; asks hourly rate".""")

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "summary": {"type": "string"},
    },
    "required": ["category", "summary"],
}


def describe_for_model(mail):
    lines = [
        f"From: {mail.from_name} <{mail.from_addr}>",
        f"Subject: {mail.subject or '(none)'}",
    ]
    if mail.attachments:
        lines.append("Attachments: " + ", ".join(mail.attachments[:5]))
    if mail.bulk:
        lines.append("Sent as bulk/mailing-list mail: yes")
    lines.append("")
    lines.append(mail.body or "(no text)")
    return "\n".join(lines)


def classify(mail, model=None, system=None, guide=None, text=None):
    """Ask the local model for a category. Returns (category, summary) or None if it can't answer.
    system/guide/text replace the email prompts (sms.py uses them for text messages)."""
    if text is None:
        text = "Email:\n" + describe_for_model(mail)
    payload = {
        "model": model or OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": f"{guide or CATEGORY_GUIDE}\n\n{text}"},
        ],
        "format": SCHEMA,
        "stream": False,
        "keep_alive": int(OLLAMA_KEEP_ALIVE) if OLLAMA_KEEP_ALIVE.lstrip("-").isdigit() else OLLAMA_KEEP_ALIVE,
        "options": {"temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
    }
    if OLLAMA_THINK:
        payload["think"] = OLLAMA_THINK.lower() == "true"
    try:
        res = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=OLLAMA_TIMEOUT)
        res.raise_for_status()
        answer = json.loads(res.json()["message"]["content"])
        category = answer.get("category")
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        return category, str(answer.get("summary", "")).strip()[:200]
    except Exception as err:
        log.error("Model classification failed: %s", err)
        return None


# ==================== DECISIONS ==================== #

@dataclass
class Decision:
    category: str
    reason: str
    topic: str = ""
    priority: int = 3
    summary: str = ""

    @property
    def notify(self):
        return bool(self.topic)


TOPIC_FOR = {
    "quote_request": (NTFY_QUOTE_TOPIC, 4),
    "customer_query": (NTFY_MESSAGE_TOPIC, 4),
    "business_admin": (NTFY_MESSAGE_TOPIC, 1),  # silent, but kept in the alert history
}


def decide(mail, contacts, sent_ids, model=None):
    if mail.from_addr in OWN_ADDRESSES or (OWN_DOMAINS and from_domain(mail, *OWN_DOMAINS)):
        return Decision("own", "sent from our own address")

    contact = contacts.get(mail.from_addr)
    replying = bool(mail.reply_to_ids & sent_ids)

    if not contact and not replying:
        rule = spam_rule(mail)
        if rule:
            return Decision("spam", f"spam rule: {rule}")
        if re.search(r"(?i)\bquote request\b", mail.subject):
            return Decision("quote_request", "subject says quote request (real estate)", NTFY_QUOTE_TOPIC, 3,
                            summary="Real estate / strata quote request")

    result = classify(mail, model)
    if result is None:
        # Better a notification too many than a missed customer
        return Decision("unclassified", "model unavailable", NTFY_MESSAGE_TOPIC, 3)
    category, summary = result

    if contact or replying:
        reason = f"contact: {contact}" if contact else "reply to our email"
        topic, priority = TOPIC_FOR.get(category, (NTFY_MESSAGE_TOPIC, 3))
        return Decision(category, reason, topic, max(priority, 3), summary)

    topic, priority = TOPIC_FOR.get(category, ("", 3))
    return Decision(category, "model", topic, priority, summary)


TITLES = {
    "quote_request": "Quote request",
    "customer_query": "Customer email",
    "business_admin": "Business admin",
    "unclassified": "Email (not sorted)",
}


def notify(mail, decision):
    sender = mail.from_name or mail.from_addr
    title = f"{TITLES.get(decision.category, 'Email')}: {sender}"
    lines = [f"From: {mail.from_name} <{mail.from_addr}>" if mail.from_name else f"From: {mail.from_addr}"]
    if mail.to:
        lines.append(f"To: {mail.to}")
    lines.append(f"Subject: {mail.subject or '(none)'}")
    if decision.summary:
        lines.append("")
        lines.append(decision.summary)
    if decision.reason.startswith(("contact", "reply")):
        lines.append(f"({decision.reason})")
    tags = ["email"] + (["warning"] if decision.category == "unclassified" else [])
    payload = {"topic": decision.topic, "title": title, "message": "\n".join(lines),
               "priority": decision.priority, "tags": tags}
    headers = {"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}
    res = requests.post(NTFY_URL + "/", json=payload, headers=headers, timeout=15)
    res.raise_for_status()


# ==================== CONTACTS ==================== #

class Contacts:
    """Email address -> display name, from the Radicale address books. Refreshed periodically."""

    def __init__(self):
        self.by_email = {}
        self.loaded_at = 0

    def get(self, address):
        self.refresh()
        return self.by_email.get(address)

    def refresh(self, force=False):
        if not CONTACT_BOOKS or (not force and time.time() - self.loaded_at < CONTACTS_REFRESH_SECONDS):
            return
        found = {}
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<c:addressbook-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
                '<d:prop><c:address-data/></d:prop></c:addressbook-query>')
        try:
            for book in CONTACT_BOOKS:
                path = "/" + book.strip().strip("/") + "/"
                res = requests.request("REPORT", RADICALE_URL + path, data=body, timeout=20,
                                       auth=(RADICALE_USER, RADICALE_PASS),
                                       headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
                res.raise_for_status()
                found.update(parse_vcards(html.unescape(res.text)))
        except Exception as err:
            # Keep the previous list rather than forgetting everyone
            log.error("Could not load contacts: %s", err)
            self.loaded_at = time.time() - CONTACTS_REFRESH_SECONDS + 120
            return
        self.by_email = found
        self.loaded_at = time.time()
        log.info("Loaded %d contact email addresses", len(found))


def parse_vcards(text):
    found = {}
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold
    for card in re.findall(r"BEGIN:VCARD.*?END:VCARD", text, re.S):
        name = re.search(r"^FN[^:\n]*:(.*)$", card, re.M)
        name = name.group(1).strip().replace("\\,", ",") if name else ""
        for addr in re.findall(r"^(?:item\d+\.)?EMAIL[^:\n]*:(.*)$", card, re.M | re.I):
            addr = addr.strip().lower()
            if addr:
                found[addr] = name or addr
    return found


# ==================== IMAP ==================== #

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    directory = os.path.dirname(STATE_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as f:
        json.dump(state, f)
        tmp = f.name
    os.replace(tmp, STATE_FILE)


def account_login(name):
    user = env(f"IMAP_{name.upper()}_USER")
    password = env(f"IMAP_{name.upper()}_PASS")
    if not user or not password:
        raise RuntimeError(f"IMAP_{name.upper()}_USER / IMAP_{name.upper()}_PASS not set")
    if IMAP_TLS == "ssl":
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=60)
    else:
        imap = imaplib.IMAP4(IMAP_HOST, IMAP_PORT, timeout=60)
        if IMAP_TLS == "starttls":
            imap.starttls()
    imap.login(user, password)
    return imap


def mailbox_status(imap, mailbox):
    typ, data = imap.status(f'"{mailbox}"', "(UIDVALIDITY UIDNEXT)")
    if typ != "OK":
        raise RuntimeError(f"STATUS {mailbox} failed")
    text = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
    validity = int(re.search(r"UIDVALIDITY (\d+)", text).group(1))
    uidnext = int(re.search(r"UIDNEXT (\d+)", text).group(1))
    return validity, uidnext


def uids_after(imap, last_uid):
    typ, data = imap.uid("search", None, f"UID {last_uid + 1}:*")
    if typ != "OK":
        raise RuntimeError("UID SEARCH failed")
    # "n:*" always matches the newest message, even when its UID is below n
    return sorted(u for u in (int(x) for x in data[0].split()) if u > last_uid)


def fetch_raw(imap, uid, part="BODY.PEEK[]"):
    typ, data = imap.uid("fetch", str(uid), f"({part})")
    if typ != "OK":
        raise RuntimeError(f"FETCH {uid} failed")
    for item in data:
        if isinstance(item, tuple):
            return item[1]
    return b""


class SentIds:
    """Message-IDs of mail we sent, so replies to it can be recognised."""

    def __init__(self):
        self.ids = {}       # account -> set of Message-IDs
        self.last_uid = {}  # account -> highest Sent UID seen
        self.validity = {}

    def refresh(self, imap, account):
        try:
            validity, _ = mailbox_status(imap, SENT_FOLDER)
            if self.validity.get(account) != validity:
                self.ids[account], self.last_uid[account], self.validity[account] = set(), 0, validity
            imap.select(f'"{SENT_FOLDER}"', readonly=True)
            if self.last_uid[account]:
                uids = uids_after(imap, self.last_uid[account])
            else:
                since = time.strftime("%d-%b-%Y", time.localtime(time.time() - SENT_DAYS * 86400))
                typ, data = imap.uid("search", None, f"SINCE {since}")
                uids = sorted(int(x) for x in data[0].split()) if typ == "OK" else []
            for uid in uids:
                header = fetch_raw(imap, uid, "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]").decode(errors="replace")
                self.ids[account] |= message_ids(header)
                self.last_uid[account] = max(self.last_uid[account], uid)
        except Exception as err:
            log.warning("Could not read %s folder for %s: %s", SENT_FOLDER, account, err)

    def all(self):
        return set().union(*self.ids.values()) if self.ids else set()


def check_account(name, state, contacts, sent):
    imap = account_login(name)
    try:
        sent.refresh(imap, name)
        validity, uidnext = mailbox_status(imap, "INBOX")
        box = state.setdefault(name, {})
        if box.get("uidvalidity") != validity:
            # First run, or the mailbox was rebuilt: start from now rather than replaying the inbox
            log.info("%s: starting from UID %d", name, uidnext - 1)
            state[name] = {"uidvalidity": validity, "last_uid": uidnext - 1}
            save_state(state)
            return 0

        imap.select("INBOX", readonly=True)
        uids = uids_after(imap, box["last_uid"])[:BATCH_LIMIT]
        sent_ids = sent.all()
        for uid in uids:
            mail = parse_mail(fetch_raw(imap, uid), uid, name)
            decision = decide(mail, contacts, sent_ids)
            log.info("%s uid %d from %s -> %s (%s)", name, uid, mail.from_addr, decision.category, decision.reason)
            if decision.notify:
                notify(mail, decision)
            box["last_uid"] = uid
            save_state(state)
        return len(uids)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def push_health(status, message):
    if not KUMA_PUSH_URL:
        return
    try:
        requests.get(KUMA_PUSH_URL, params={"status": status, "msg": message[:200]}, timeout=10)
    except Exception as err:
        log.warning("Uptime Kuma push failed: %s", err)


def main():
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    log.info("Prompts from %s: %s", PROMPTS_DIR, ", ".join(PROMPTS_FROM_FILES) or "none, using built-in")
    log.info("mail-triage watching %s with model %s", ", ".join(ACCOUNTS), OLLAMA_MODEL)
    state = load_state()
    contacts = Contacts()
    sent = SentIds()

    while True:
        errors = []
        handled = 0
        for name in ACCOUNTS:
            try:
                handled += check_account(name, state, contacts, sent)
            except Exception as err:
                log.error("%s: %s", name, err)
                errors.append(f"{name}: {err}")
        if errors:
            push_health("down", "; ".join(errors))
        else:
            push_health("up", f"OK, {handled} new")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
