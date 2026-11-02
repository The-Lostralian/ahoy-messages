"""
Test harness for mail-triage. Nothing here sends notifications or changes mail.

Compare models on the sample emails (samples.jsonl):
    python harness.py --models qwen2.5:3b,qwen3:4b,gemma3:4b,phi4-mini

Dry-run the real decision on the newest inbox messages (read-only):
    python harness.py --inbox 30 --account main
"""

import argparse
import json
import time
from pathlib import Path

import triage


def sample_mail(sample):
    from_name, from_addr = triage.parseaddr(sample["from"])
    return triage.Mail(
        uid=0,
        account="sample",
        from_name=from_name,
        from_addr=from_addr.lower(),
        to="",
        subject=sample.get("subject", ""),
        body=sample.get("body", ""),
        attachments=sample.get("attachments", []),
        bulk=sample.get("bulk", False),
    )


def run_samples(models, path):
    samples = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    contacts = triage.Contacts()  # empty: samples are judged as if from strangers
    width = max(len(s["name"]) for s in samples)

    for model in models:
        print(f"\n=== {model}")
        correct, alerted_right, model_calls, model_secs = 0, 0, 0, 0.0
        for sample in samples:
            mail = sample_mail(sample)
            start = time.time()
            decision = triage.decide(mail, contacts, set(), model=model)
            secs = time.time() - start
            if decision.reason == "model":
                model_calls += 1
                model_secs += secs
            ok = decision.category == sample["expect"]
            correct += ok
            # The thing that matters most: did it alert (or stay quiet) when it should?
            should_alert = sample["expect"] in triage.TOPIC_FOR
            alert_ok = decision.notify == should_alert
            alerted_right += alert_ok
            mark = "ok  " if ok else ("near" if alert_ok else "MISS")
            print(f"{mark} {sample['name']:<{width}}  expected {sample['expect']:<14} got {decision.category:<14} "
                  f"[{decision.reason}] {secs:5.1f}s  {decision.summary}")
        avg = model_secs / model_calls if model_calls else 0
        print(f"--- {model}: {correct}/{len(samples)} exact, {alerted_right}/{len(samples)} alerted correctly "
              f"('near' = right alert, wrong category), {model_calls} model calls, {avg:.1f}s average")
        if model != triage.OLLAMA_MODEL:
            unload(model)


def unload(model):
    """Free the GPU for the next model (the live service keeps its own model loaded)."""
    try:
        triage.requests.post(f"{triage.OLLAMA_URL}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except Exception as err:
        print(f"(could not unload {model}: {err})")


def run_inbox(account, count, model):
    contacts = triage.Contacts()
    sent = triage.SentIds()
    imap = triage.account_login(account)
    try:
        sent.refresh(imap, account)
        imap.select("INBOX", readonly=True)
        typ, data = imap.uid("search", None, "ALL")
        uids = sorted(int(x) for x in data[0].split())[-count:]
        for uid in uids:
            mail = triage.parse_mail(triage.fetch_raw(imap, uid), uid, account)
            decision = triage.decide(mail, contacts, sent.all(), model=model)
            action = f"-> {decision.topic} (p{decision.priority})" if decision.notify else "-> no alert"
            print(f"uid {uid:<6} {mail.from_addr[:40]:<40} {mail.subject[:45]:<45} "
                  f"{decision.category:<14} {action}  [{decision.reason}] {decision.summary}")
    finally:
        imap.logout()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default=triage.OLLAMA_MODEL, help="comma-separated Ollama models to compare")
    parser.add_argument("--samples", default=str(Path(__file__).with_name("samples.jsonl")))
    parser.add_argument("--inbox", type=int, metavar="N", help="dry-run on the newest N inbox messages instead")
    parser.add_argument("--account", default=triage.ACCOUNTS[0])
    args = parser.parse_args()

    if args.inbox:
        run_inbox(args.account, args.inbox, args.models.split(",")[0])
    else:
        run_samples([m.strip() for m in args.models.split(",") if m.strip()], args.samples)


if __name__ == "__main__":
    main()
