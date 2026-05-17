#!/usr/bin/env python3
"""
Email Digest → Telegram  — Naturdao / Body Nostrum LLC
======================================================
Llegeix emails de les últimes 24h dels comptes actius de naturdao.com,
genera un resum per compte amb Claude Haiku i l'envia per Telegram.
Cron Railway: 0 7 * * *  (07:00 UTC = 09:00 CEST)

Variables d'entorn necessàries:
  IMAP_HOST          (default: s6correo.profesionalhosting.com)
  IMAP_PORT          (default: 143 STARTTLS)
  EMAIL_ACCOUNTS     JSON: [{"email":"soport@naturdao.com","password":"xxx"}, ...]
  ANTHROPIC_API_KEY
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID
"""

import imaplib
import email
import os
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import decode_header

# ─── CONFIG ─────────────────────────────────────────────────────────────────
IMAP_HOST         = os.environ.get("IMAP_HOST", "s6correo.profesionalhosting.com")
IMAP_PORT         = int(os.environ.get("IMAP_PORT", "143"))
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
ACCOUNTS_RAW      = os.environ.get("EMAIL_ACCOUNTS", "[]")
LOOKBACK_HOURS    = int(os.environ.get("LOOKBACK_HOURS", "24"))


# ─── HELPERS ────────────────────────────────────────────────────────────────
def decode_str(s):
    if not s:
        return ""
    parts = decode_header(s)
    result = []
    for decoded, enc in parts:
        if isinstance(decoded, bytes):
            try:
                result.append(decoded.decode(enc or "utf-8", errors="replace"))
            except Exception:
                result.append(decoded.decode("latin-1", errors="replace"))
        else:
            result.append(decoded or "")
    return " ".join(result).strip()


def get_body(msg, max_chars=600):
    """Extreu el cos de text pla (màx max_chars caràcters)."""
    if msg.is_multipart():
        for part in msg.walk():
            if (part.get_content_type() == "text/plain"
                    and "attachment" not in str(part.get("Content-Disposition", ""))):
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )[:max_chars].strip()
                except Exception:
                    pass
    else:
        try:
            return msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )[:max_chars].strip()
        except Exception:
            pass
    return ""


# ─── IMAP ───────────────────────────────────────────────────────────────────
def fetch_recent_emails(account_email, password, hours=24):
    """Retorna llista de dicts amb els emails de les últimes N hores."""
    emails = []
    try:
        M = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        M.starttls()
        M.login(account_email, password)
        M.select("INBOX")

        since_date = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%d-%b-%Y")
        _, data = M.search(None, f"SINCE {since_date}")
        msg_ids = data[0].split() if data[0] else []

        for msg_id in msg_ids[-60:]:   # màx 60 per compte
            try:
                _, msg_data = M.fetch(msg_id, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                emails.append({
                    "from":    decode_str(msg.get("From", "")),
                    "subject": decode_str(msg.get("Subject", "(sense assumpte)")),
                    "date":    msg.get("Date", ""),
                    "preview": get_body(msg),
                })
            except Exception as e:
                print(f"    ⚠  Error llegint missatge {msg_id}: {e}")
                continue

        M.logout()
        print(f"  ✓ {account_email}: {len(emails)} email(s) trobats")

    except imaplib.IMAP4.error as e:
        print(f"  ✗ IMAP error {account_email}: {e}")
    except Exception as e:
        print(f"  ✗ Error {account_email}: {e}")

    return emails


# ─── CLAUDE HAIKU ───────────────────────────────────────────────────────────
def summarize(account, emails):
    """Resum breu dels emails via Claude Haiku. Retorna string."""
    if not emails:
        return None

    emails_text = "\n\n".join(
        f"De: {e['from']}\nAssumpte: {e['subject']}\nPrevisualització: {e['preview']}"
        for e in emails
    )

    prompt = (
        f"Ets l'assistent de Naturdao (productes de salut natural). "
        f"Analitza els {len(emails)} emails rebuts a {account} en les últimes 24h.\n"
        f"Identifica: comandes noves, incidències, consultes urgents, emails de proveïdors/Amazon, accions pendents.\n"
        f"Respon en català, molt concís, màx 5 punts amb •. Si no hi ha res urgent, indica-ho.\n\n"
        f"EMAILS:\n{emails_text}\n\nRESUM:"
    )

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 450,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            resp = json.loads(r.read())
            return resp["content"][0]["text"].strip()
    except Exception as e:
        print(f"  ⚠  Error Claude API: {e}")
        return f"[Error generant resum: {e}]"


# ─── TELEGRAM ───────────────────────────────────────────────────────────────
def send_telegram(text):
    # Telegram màx 4096 chars per missatge
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        body = json.dumps({
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       chunk,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status != 200:
                    print(f"  ⚠  Telegram HTTP {r.status}")
        except Exception as e:
            print(f"  ✗ Error Telegram: {e}")
            raise


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    print(f"\n{'='*50}")
    print(f"📧 Email Digest Naturdao — {now_str}")
    print(f"{'='*50}\n")

    try:
        accounts = json.loads(ACCOUNTS_RAW)
    except json.JSONDecodeError:
        print("✗ ERROR: EMAIL_ACCOUNTS no és JSON vàlid")
        exit(1)

    if not accounts:
        print("✗ ERROR: cap compte configurat a EMAIL_ACCOUNTS")
        exit(1)

    parts   = [f"📧 *Digest Naturdao — {now_str}*\n"]
    total   = 0
    errors  = 0

    for acc in accounts:
        acc_email = acc.get("email", "")
        password  = acc.get("password", "")
        if not acc_email or not password:
            continue

        emails = fetch_recent_emails(acc_email, password, hours=LOOKBACK_HOURS)
        n = len(emails)
        total += n

        if n == 0:
            parts.append(f"📭 *{acc_email}*: cap email nou\n")
        else:
            summary = summarize(acc_email, emails)
            if summary:
                parts.append(f"📬 *{acc_email}* \\({n}\\)\n{summary}\n")
            else:
                parts.append(f"📬 *{acc_email}*: {n} email(s) (sense resum)\n")
                errors += 1

    status_emoji = "✅" if errors == 0 else "⚠️"
    parts.append(f"\n_{status_emoji} {total} emails processats · {now_str}_")

    digest = "\n".join(parts)
    print(f"\n--- DIGEST ---\n{digest}\n")

    send_telegram(digest)
    print(f"\n✅ Digest enviat ({total} emails, {errors} error(s))")


if __name__ == "__main__":
    main()
