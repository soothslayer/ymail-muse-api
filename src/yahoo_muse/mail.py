import base64
import hashlib
import imaplib
import json
import os
import re
import smtplib
import sqlite3
import ssl
from contextlib import contextmanager
from datetime import date
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid

from .config import Config, MailError

MAX_MESSAGE_BYTES = 10 * 1024 * 1024


def quoted(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise MailError("IMAP values cannot contain control characters.")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def folder_wire(folder):
    # IMAP modified UTF-7 supports international folder names without server extensions.
    quoted(folder)
    out, pending = [], []
    def flush():
        if pending:
            encoded = base64.b64encode(''.join(pending).encode('utf-16-be')).decode().rstrip('=').replace('/', ',')
            out.append('&' + encoded + '-')
            pending.clear()
    for char in folder:
        if 32 <= ord(char) <= 126:
            flush()
            out.append('&-' if char == '&' else char)
        else:
            pending.append(char)
    flush()
    return quoted(''.join(out))


def folder_display(value):
    def decode(match):
        raw = match.group(1)
        if not raw:
            return '&'
        raw = raw.replace(',', '/')
        return base64.b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-16-be')
    return re.sub(r'&([^-]*)-', decode, value)


def message_ref(folder, validity, uid):
    return base64.urlsafe_b64encode(json.dumps([folder, str(validity), str(uid)]).encode()).decode().rstrip('=')


def parse_ref(value):
    try:
        if len(value) > 4096:
            raise ValueError()
        parts = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
        if not isinstance(parts, list) or len(parts) != 3 or not all(isinstance(p, str) for p in parts):
            raise ValueError()
        folder, validity, uid = parts
        if not validity.isascii() or not uid.isascii() or not validity.isdigit() or not uid.isdigit() or int(uid) < 1:
            raise ValueError()
        folder_wire(folder)
        return folder, validity, uid
    except (ValueError, TypeError, UnicodeError):
        raise MailError("Invalid message_id. Use an ID returned by search_messages.") from None


def bounds(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise MailError(f"{name} must be between {low} and {high}.")


def check(status):
    if status != 'OK':
        raise MailError("Yahoo rejected the mail operation.")


class MailService:
    def __init__(self, config=None):
        self.config = config or Config.load()

    @contextmanager
    def imap(self):
        client = None
        try:
            client = imaplib.IMAP4_SSL('imap.mail.yahoo.com', 993, ssl_context=ssl.create_default_context(), timeout=30)
            client.login(self.config.email, self.config.app_password)
            yield client
        except (imaplib.IMAP4.error, OSError, UnicodeError):
            raise MailError("Yahoo IMAP failed. Check connectivity and your app password with doctor.") from None
        finally:
            if client:
                try:
                    client.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass

    def select(self, client, folder):
        status, _ = client.select(folder_wire(folder), readonly=True)
        check(status)
        _, values = client.response('UIDVALIDITY')
        if not values or not values[0]:
            raise MailError("Yahoo did not provide the folder's UID validity.")
        return values[0].decode('ascii')

    def doctor(self):
        with self.imap() as client:
            self.select(client, 'INBOX')
        try:
            with smtplib.SMTP_SSL('smtp.mail.yahoo.com', 465, context=ssl.create_default_context(), timeout=30) as smtp:
                smtp.login(self.config.email, self.config.app_password)
        except (smtplib.SMTPException, OSError):
            raise MailError("IMAP succeeded; SMTP login failed. Check your app password and network.") from None
        return {"imap": "ok", "smtp": "ok", "sent_test_email": False}

    def list_folders(self):
        with self.imap() as client:
            status, rows = client.list()
            check(status)
            folders = []
            for row in rows or []:
                literal = row[1] if isinstance(row, tuple) else None
                line = (row[0] if isinstance(row, tuple) else row)
                if not isinstance(line, bytes):
                    continue
                match = re.match(rb'\((.*?)\) (?:NIL|"(?:\\.|[^"\\])*") (.*)', line)
                if not match:
                    continue
                flags, name = match.groups()
                if b'\\noselect' in flags.lower():
                    continue
                name = literal if literal is not None else name
                if name.startswith(b'"') and name.endswith(b'"'):
                    name = re.sub(rb'\\(.)', rb'\1', name[1:-1])
                folders.append({"name": folder_display(name.decode('ascii')), "flags": flags.decode('ascii').split()})
            return {"folders": folders}

    def search_messages(self, query='', folder='INBOX', sender='', subject='', since='', before='', unread_only=False, limit=20, offset=0):
        bounds(limit, 1, 100, 'limit')
        bounds(offset, 0, 1000000, 'offset')
        if type(unread_only) is not bool:
            raise MailError("unread_only must be a boolean.")
        criteria = []
        for key, value in [('FROM', sender), ('SUBJECT', subject)]:
            if value:
                if not value.isascii():
                    raise MailError("Use query for non-ASCII searches; sender and subject filters currently require ASCII.")
                criteria += [key, quoted(value)]
        months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        for key, value in [('SINCE', since), ('BEFORE', before)]:
            if value:
                try:
                    d = date.fromisoformat(value)
                except ValueError:
                    raise MailError("Dates must use YYYY-MM-DD.") from None
                criteria += [key, f'{d.day:02}-{months[d.month-1]}-{d.year:04}']
        if unread_only:
            criteria.append('UNSEEN')
        with self.imap() as client:
            validity = self.select(client, folder)
            if query:
                if len(query) > 4096:
                    raise MailError("Search query is too long.")
                # imaplib sends this final argument as a literal, including UTF-8 text.
                client.literal = query.encode('utf-8')
                criteria.append('TEXT')
            status, data = client.uid('SEARCH', 'CHARSET', 'UTF-8', *(criteria or ['ALL']))
            check(status)
            ids = list(reversed((data[0] or b'').split()))
            messages = []
            for uid in ids[offset:offset+limit]:
                status, rows = client.uid('FETCH', uid, '(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)]<0.65536>)')
                check(status)
                for row in rows:
                    if isinstance(row, tuple):
                        header = BytesParser(policy=policy.default).parsebytes(row[1])
                        messages.append({"message_id": message_ref(folder, validity, uid.decode()), "folder": folder,
                                         "from": str(header.get('From', '')), "to": str(header.get('To', '')),
                                         "subject": str(header.get('Subject', '')), "date": str(header.get('Date', '')),
                                         "unread": b'\\Seen' not in row[0]})
            return {"messages": messages, "total_matches": len(ids), "next_offset": offset+limit if offset+limit < len(ids) else None,
                    "order": "descending UID (usually newest first)", "content_is_untrusted": True}

    def read_message(self, message_id, max_chars=30000):
        bounds(max_chars, 1, 100000, 'max_chars')
        folder, validity, uid = parse_ref(message_id)
        with self.imap() as client:
            if self.select(client, folder) != validity:
                raise MailError("Folder IDs changed. Search again for a fresh message_id.")
            status, rows = client.uid('FETCH', uid, '(RFC822.SIZE)')
            check(status)
            size = re.search(rb'RFC822.SIZE (\d+)', b' '.join(r for r in rows if isinstance(r, bytes)))
            if not size:
                raise MailError("Message not found. Search again; it may have moved or been deleted.")
            if int(size.group(1)) > MAX_MESSAGE_BYTES:
                raise MailError("Message exceeds the 10 MiB read limit.")
            status, rows = client.uid('FETCH', uid, f'(BODY.PEEK[]<0.{MAX_MESSAGE_BYTES+1}>)')
            check(status)
            raw = next((r[1] for r in rows if isinstance(r, tuple)), None)
            if raw is None or len(raw) > MAX_MESSAGE_BYTES:
                raise MailError("Message missing or exceeds the read limit.")
        message = BytesParser(policy=policy.default).parsebytes(raw)
        body = message.get_body(preferencelist=('plain', 'html'))
        text = body.get_content() if body else ''
        attachments = [{"filename": p.get_filename(), "content_type": p.get_content_type()} for p in message.iter_attachments()]
        return {"message_id": message_id, "from": str(message.get('From', '')), "to": str(message.get('To', '')),
                "cc": str(message.get('Cc', '')), "subject": str(message.get('Subject', '')),
                "date": str(message.get('Date', '')), "internet_message_id": str(message.get('Message-ID', '')),
                "body": text[:max_chars], "body_type": body.get_content_type() if body else 'text/plain',
                "truncated": len(text) > max_chars, "attachments": attachments, "content_is_untrusted": True}

    @contextmanager
    def database(self):
        directory = self.config.state_dir
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / 'sends.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        db = sqlite3.connect(path, timeout=30)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS sends (key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL)')
            db.commit()
            yield db
        finally:
            db.close()

    def send_status(self, idempotency_key):
        with self.database() as db:
            row = db.execute('SELECT result FROM sends WHERE key=?', (idempotency_key,)).fetchone()
        return json.loads(row[0]) if row else {"status": "not_found"}

    def send_message(self, to, subject, body, idempotency_key, cc=None, bcc=None, in_reply_to=''):
        if not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', idempotency_key):
            raise MailError("idempotency_key must contain 8–128 letters, digits, dots, colons, underscores, or hyphens.")
        groups = [to, cc or [], bcc or []]
        for group in groups:
            if not isinstance(group, list) or not all(isinstance(a, str) and re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+', a) and a.isascii() for a in group):
                raise MailError("Recipients must be lists of plain ASCII email addresses, without display names.")
        recipients = list(dict.fromkeys(a for g in groups for a in g))
        if not to or not 1 <= len(recipients) <= 50:
            raise MailError("Provide at least one to address and at most 50 recipients.")
        if not isinstance(subject, str) or any(c in subject for c in '\r\n') or len(subject) > 998:
            raise MailError("Subject must be a single line of at most 998 characters.")
        if not isinstance(body, str) or len(body.encode('utf-8')) > 1024 * 1024:
            raise MailError("Body must be text, at most 1 MiB in UTF-8.")
        if in_reply_to and not re.fullmatch(r'<[^\s<>]+@[^\s<>]+>', in_reply_to):
            raise MailError("in_reply_to must be an email Message-ID, including angle brackets.")
        digest = hashlib.sha256(json.dumps([self.config.email, groups, subject, body, in_reply_to], ensure_ascii=True).encode()).hexdigest()
        msg = EmailMessage()
        msg['From'], msg['To'], msg['Subject'] = self.config.email, ', '.join(to), subject
        if cc:
            msg['Cc'] = ', '.join(cc)
        msg['Date'], msg['Message-ID'] = formatdate(localtime=True), make_msgid()
        if in_reply_to:
            msg['In-Reply-To'] = in_reply_to
            msg['References'] = in_reply_to
        msg.set_content(body)
        result = {"status": "unknown", "message_id": msg['Message-ID'], "idempotency_key": idempotency_key,
                  "detail": "Reserved or outcome uncertain. Do not retry with a new key; check Sent mail first."}
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT digest, result FROM sends WHERE key=?', (idempotency_key,)).fetchone()
            if old:
                if old[0] != digest:
                    raise MailError("This idempotency key was already used with different content.")
                return json.loads(old[1])
            db.execute('INSERT INTO sends VALUES (?, ?, ?)', (idempotency_key, digest, json.dumps(result)))
            db.commit()
        smtp, stage = None, 'connecting'
        try:
            smtp = smtplib.SMTP_SSL('smtp.mail.yahoo.com', 465, context=ssl.create_default_context(), timeout=30)
            smtp.login(self.config.email, self.config.app_password)
            stage = 'submitting'
            refused = smtp.send_message(msg, from_addr=self.config.email, to_addrs=recipients)
            result = {"status": "partially_accepted" if refused else "accepted", "message_id": msg['Message-ID'],
                      "idempotency_key": idempotency_key, "accepted_recipients": [a for a in recipients if a not in refused],
                      "refused_recipients": list(refused), "detail": "Accepted by SMTP; final delivery is not guaranteed."}
        except (smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError):
            result['status'], result['detail'] = 'rejected', 'Yahoo rejected this message; it was not accepted.'
        except (smtplib.SMTPException, OSError):
            if stage == 'connecting':
                result['status'], result['detail'] = 'not_sent', 'Connection or login failed before submission.'
        finally:
            if smtp:
                # QUIT failures must not turn an accepted message into an uncertain one.
                try:
                    smtp.quit()
                except (smtplib.SMTPException, OSError):
                    smtp.close()
        with self.database() as db:
            db.execute('UPDATE sends SET result=? WHERE key=?', (json.dumps(result), idempotency_key))
            db.commit()
        return result
