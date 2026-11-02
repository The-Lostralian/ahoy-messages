# Your own prompts

The server has generic built-in prompts for "a small local business". To use your own, make a
`prompts` folder next to `docker-compose.yml` (it's mounted at `/prompts`) and put any of these
files in it. Each file replaces one built-in prompt; files you leave out keep the built-in one.
Keep the folder private: it's where your business details and writing rules go.

| File | Used for |
|---|---|
| `email_system.txt` | Who the email sorter works for, e.g. your business, where it is, what work you do |
| `categories.txt` | The category guide shown with every email and text. Must describe the six categories by these exact names: `quote_request`, `customer_query`, `business_admin`, `marketing`, `spam`, `personal` |
| `sms_system.txt` | Who the text message sorter works for |
| `sms_categories.txt` | Extra rules added after `categories.txt` for text messages (scam texts, wrong numbers) |
| `draft_system.txt` | How reply drafts are written: your voice, length, spelling, what never to make up |
| `draft_style_default.txt` | Style to use before any conversations have been reviewed |
| `review_system.txt` | How notes about a person are written during a style review |

You don't need to ask for JSON or describe the answer format: the server holds the model to it.
The server logs which files it loaded when it starts (`Prompts from /prompts: ...`).

`draft_system.txt` here is an example.
