# Ahoy server

One small Python image, two services. Both use the same local model (through Ollama) and push what
matters to [ntfy](https://ntfy.sh). Either one runs fine without the other.

| Service | What it does | Started with |
|---|---|---|
| `sms-triage` | The phone app's server: sorts incoming texts, writes reply drafts, learns your style | `python sms.py` |
| `mail-triage` | Watches one or more email inboxes and alerts you about the emails that matter | `python triage.py` (the default) |

Setup steps for the server as a whole are in the [main README](../README.md#setting-up-the-server). This page
covers email sorting, the ntfy alerts, and the settings.

## Email sorting (`mail-triage`)

It reads your inboxes over IMAP and never changes them: mailboxes are opened read-only and messages are
fetched without being marked as read. It only looks at mail that arrives after its first start.

For each new email, in this order:

1. **From one of your own addresses or domains** (`OWN_ADDRESSES`, `OWN_DOMAINS`): ignored. Useful for
   website form confirmations and summaries you send yourself.
2. **From someone in your address book, or a reply to an email you sent**: always alerted. Replies are
   recognised from your Sent folder.
3. **Matches a spam rule** (outsourced estimating, SEO, freelancers, supplier invitations and similar):
   ignored, without asking the model.
4. **"Quote request" in the subject** (how real estate and strata portals send work): alerted as a quote.
5. **Everything else**: the model picks a category: quote request, customer message, business admin,
   marketing, spam or personal.

What you need:

- **Any IMAP account**: Gmail, Fastmail, your hosting provider's mail, your own mail server. It reads mail
  only and never sends, so it needs IMAP, not SMTP. Most providers want `IMAP_PORT=993` with `IMAP_TLS=ssl`;
  Gmail and some others need an app password rather than your normal one.
- **Proton Mail** has no IMAP of its own: run [Proton Mail Bridge](https://proton.me/mail/bridge) in a
  container (several community images exist) on the same Docker network, and use port 143 with
  `IMAP_TLS` left empty and the bridge password. Bridge offers STARTTLS but with its own self-signed
  certificate, which this won't accept, so leave it plain; that traffic never leaves the Docker network.
- **Optionally, a CardDAV address book** such as [Radicale](https://radicale.org), so mail from your contacts
  is never missed. The same contacts can sync to your phone with DAVx⁵.

Several inboxes: list them in `IMAP_ACCOUNTS` and give each its own login, for example
`IMAP_ACCOUNTS=main,shop` with `IMAP_MAIN_USER` / `IMAP_MAIN_PASS` and `IMAP_SHOP_USER` / `IMAP_SHOP_PASS`.

To try models or prompts without sending any alerts:

```bash
docker compose run --rm mail-triage python harness.py --models qwen3:4b
docker compose run --rm mail-triage python harness.py --inbox 30 --account main
```

The first scores the model against the example emails in `samples.jsonl`; the second shows what it
would have done with your 30 newest emails.

## Text sorting (`sms-triage`)

For each text the phone receives:

- **One-time codes** are never filtered or alerted.
- **From a contact:** the phone notifies you as usual. It's also sent to ntfy if it's a quote, a customer
  message or business admin.
- **From a stranger:** spam rules, then the model. Spam and marketing go to the app's *Filtered* list
  without a notification. Quotes and customer messages are alerted.
- If the model can't be reached, texts from strangers are alerted anyway (better one too many than a
  missed customer), and the phone shows them as normal.

Drafts and style reviews are covered in the [main README](../README.md).

## ntfy alerts

Both services publish to the same two topics, so one subscription covers email and texts:

| Topic | What arrives | Priority |
|---|---|---|
| `ahoy-quote` | Quote requests | **4 (high)**; 3 for "quote request" emails from portals |
| `ahoy-webmsg` | Customer messages | **4 (high)** |
| `ahoy-webmsg` | Business admin (tax, insurance, bank, licensing) | **1 (min)**: silent, but kept in the topic's history |
| `ahoy-webmsg` | Anything from your contacts, or replies to your email, that isn't a quote or customer message | 3 (default) |
| `ahoy-webmsg` | "Not sorted": the model couldn't be reached | 3, tagged ⚠️ |

Every alert has a title such as `Quote request: Sam Lee` or `Customer text: 0412…`, a one-line summary
from the model, and a tag (`email` or `sms`) so you can tell where it came from. Message text from texts is
summarised, never copied in full. Rename the topics with `NTFY_QUOTE_TOPIC` and `NTFY_MESSAGE_TOPIC`.

Because it's plain ntfy, you can build your own alert setups on top:

- **Phone, desktop and web:** subscribe to both topics in the ntfy app or web app. Set the topics'
  notification sounds separately, so a quote sounds different from everything else.
- **Watches and wearables** get them through the phone's ntfy app.
- **Desk or wall displays:** anything that can subscribe to ntfy (an old e-reader, a Raspberry Pi
  screen, a smart display) can show new quotes. Use the priority to decide what stays on screen: for
  example, keep priority 4 and above until it's dismissed, and show the rest briefly.
- **Home Assistant, scripts and other automations:** subscribe over HTTP, WebSocket or SSE and act on the
  title, tags or priority. The [ntfy docs](https://docs.ntfy.sh/subscribe/api/) show how.
- **Your own other services** can post to the same topics, so all your business alerts land in one place.

Give the services their own ntfy user that can only publish to these topics, and use its token as
`NTFY_TOKEN`:

```bash
ntfy user add triage
ntfy access triage 'ahoy-*' write-only
ntfy token add triage
```

## Settings

All are environment variables; see `docker-compose.example.yml` and `.env.example`.

| Variable | Used by | Default | Meaning |
|---|---|---|---|
| `SMS_KEY` | sms | (required) | The key shared with the phone: 32 random bytes, base64 |
| `OWNER_NAME` | both | `Owner` | Your name as the model sees it in conversations |
| `PROMPTS_DIR` | both | `/prompts` | Your own prompts (see [`prompts.example`](prompts.example/README.md)) |
| `OLLAMA_URL` | both | `http://ollama:11434` | Your Ollama |
| `OLLAMA_MODEL` | both | `qwen2.5:3b` | The model; `qwen3:4b` is what it's tuned for |
| `OLLAMA_THINK` | both | | `false` switches off "thinking" for models like qwen3 (recommended) |
| `OLLAMA_NUM_CTX` | both | `2048` | Context size; use the same value for both services so Ollama doesn't reload the model |
| `OLLAMA_KEEP_ALIVE` | both | `-1` | How long the model stays loaded; `-1` keeps it in GPU memory |
| `NTFY_URL`, `NTFY_TOKEN` | both | `http://ntfy` | Where alerts go, and a token that can publish there |
| `NTFY_QUOTE_TOPIC`, `NTFY_MESSAGE_TOPIC` | both | `ahoy-quote`, `ahoy-webmsg` | The two topics |
| `IMAP_HOST`, `IMAP_PORT` | email | `protonmail-bridge`, `143` | Your IMAP server |
| `IMAP_TLS` | email | (plain) | `ssl` for encrypted IMAP (usually port 993), `starttls` to upgrade a port 143 connection, empty for plain (only on a private network, e.g. to Proton Bridge) |
| `IMAP_ACCOUNTS` | email | `main` | Inboxes to watch; each needs `IMAP_<NAME>_USER` and `IMAP_<NAME>_PASS` |
| `POLL_SECONDS` | email | `60` | How often to check for new mail |
| `SENT_FOLDER`, `SENT_DAYS` | email | `Sent`, `120` | Where to find your sent mail, and how far back, to recognise replies |
| `OWN_DOMAINS`, `OWN_ADDRESSES` | email | | Mail from these is ignored (comma separated) |
| `RADICALE_URL`, `RADICALE_USER`, `RADICALE_PASS`, `CONTACT_BOOKS` | email | | CardDAV address books for contacts (paths, comma separated) |
| `KUMA_PUSH_URL` | email | | Optional Uptime Kuma push monitor, pinged after each check |

`sms-triage` answers `GET /health` with `ok`, for an HTTP monitor.
