import json
import os
import smtplib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

from yahoo_muse.config import Config, MailError
from yahoo_muse.mail import MailService, folder_display, folder_wire, message_ref


class MailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config('me@yahoo.com', 'test-secret', Path(self.temp.name))
        self.service = MailService(self.config)
        self.client = MagicMock()
        self.client.select.return_value = ('OK', [b'3'])
        self.client.response.return_value = ('UIDVALIDITY', [b'42'])
        @contextmanager
        def connection():
            yield self.client
        self.service.imap = connection

    def send(self, **overrides):
        args = dict(to=['you@example.com'], subject='Hello', body='Test', idempotency_key='test-send-123')
        args.update(overrides)
        return self.service.send_message(**args)

    def test_search_utf8_literal_and_headers_without_marking_seen(self):
        self.client.uid.side_effect = [('OK', [b'1 2 3']), ('OK', [(b'3 (UID 3 FLAGS ()', b'Subject: =?utf-8?q?caf=C3=A9?=\r\nFrom: test@example.com\r\n\r\n')])]
        result = self.service.search_messages(query='café', since='2026-01-01', limit=1)
        self.assertEqual(self.client.literal, 'café'.encode())
        self.assertEqual(result['messages'][0]['subject'], 'café')
        self.assertEqual(result['next_offset'], 1)
        self.client.select.assert_called_with('"INBOX"', readonly=True)
        self.assertIn('BODY.PEEK', self.client.uid.call_args_list[1].args[2])
        self.assertEqual(result['messages'][0]['message_id'], message_ref('INBOX', '42', '3'))

    def test_stale_uid_rejected_before_fetch(self):
        with self.assertRaisesRegex(MailError, 'IDs changed'):
            self.service.read_message(message_ref('INBOX', '41', '3'))
        self.client.uid.assert_not_called()

    def test_read_mime_attachment_and_truncation(self):
        msg = EmailMessage()
        msg['Subject'] = 'test'
        msg.set_content('hello world')
        msg.add_attachment(b'private attachment', maintype='application', subtype='pdf', filename='invoice.pdf')
        self.client.uid.side_effect = [('OK', [b'3 (RFC822.SIZE 1000)']), ('OK', [(b'3 (BODY[]', msg.as_bytes())])]
        result = self.service.read_message(message_ref('INBOX', '42', '3'), max_chars=5)
        self.assertEqual(result['body'], 'hello')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['attachments'][0]['filename'], 'invoice.pdf')
        self.assertIn('BODY.PEEK', self.client.uid.call_args.args[2])

    def test_size_limit_prevents_body_fetch(self):
        self.client.uid.return_value = ('OK', [b'3 (RFC822.SIZE 999999999)'])
        with self.assertRaisesRegex(MailError, '10 MiB'):
            self.service.read_message(message_ref('INBOX', '42', '3'))
        self.assertEqual(self.client.uid.call_count, 1)

    def test_injection_and_invalid_refs(self):
        for ref in ['bad', 'e30', message_ref('INBOX', '42', '1:*')]:
            with self.assertRaises(MailError):
                self.service.read_message(ref)
        with self.assertRaises(MailError):
            self.service.search_messages(sender='a\r\nLOGOUT')
        with self.assertRaises(MailError):
            self.send(subject='hello\nBcc: victim@example.com')

    def test_international_folder_round_trip(self):
        for folder in ['INBOX', 'Travel & Receipts', '旅行', 'café']:
            self.assertEqual(folder_display(folder_wire(folder)[1:-1]), folder)

    def test_list_folders_quoted_spaces_and_literals(self):
        self.client.list.return_value = ('OK', [b'(\\Sent) "/" "Sent Messages"', b'(\\Noselect) "/" "Parent"', (b'() "/" {5}', b'Inbox')])
        result = self.service.list_folders()
        self.assertEqual([f['name'] for f in result['folders']], ['Sent Messages', 'Inbox'])

    @patch('yahoo_muse.mail.smtplib.SMTP_SSL')
    def test_send_dedup_bcc_and_quit_failure(self, factory):
        smtp = factory.return_value
        smtp.send_message.return_value = {}
        smtp.quit.side_effect = smtplib.SMTPServerDisconnected()
        first = self.send(bcc=['hidden@example.com'])
        second = self.send(bcc=['hidden@example.com'])
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'accepted')
        smtp.send_message.assert_called_once()
        sent = smtp.send_message.call_args.args[0]
        self.assertNotIn('Bcc', sent)
        self.assertIn('hidden@example.com', smtp.send_message.call_args.kwargs['to_addrs'])
        self.assertEqual(self.service.send_status('test-send-123'), first)
        with self.assertRaisesRegex(MailError, 'different content'):
            self.send(body='changed')

    @patch('yahoo_muse.mail.smtplib.SMTP_SSL')
    def test_ambiguous_send_is_not_retried(self, factory):
        factory.return_value.send_message.side_effect = TimeoutError('contains secret')
        self.assertEqual(self.send()['status'], 'unknown')
        self.assertEqual(self.send()['status'], 'unknown')
        factory.return_value.send_message.assert_called_once()
        self.assertNotIn('secret', json.dumps(self.service.send_status('test-send-123')))

    @patch('yahoo_muse.mail.smtplib.SMTP_SSL')
    def test_partial_acceptance(self, factory):
        factory.return_value.send_message.return_value = {'bad@example.com': (550, b'No such user')}
        result = self.send(to=['good@example.com', 'bad@example.com'])
        self.assertEqual(result['status'], 'partially_accepted')
        self.assertEqual(result['accepted_recipients'], ['good@example.com'])

    @patch('yahoo_muse.mail.smtplib.SMTP_SSL')
    def test_concurrent_identical_sends_submit_once(self, factory):
        factory.return_value.send_message.return_value = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.send(), range(2)))
        self.assertTrue(all(r['status'] in ['unknown', 'accepted'] for r in results))
        factory.return_value.send_message.assert_called_once()

    @patch('yahoo_muse.mail.smtplib.SMTP_SSL')
    def test_login_failure_never_submits(self, factory):
        factory.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b'bad')
        self.assertEqual(self.send()['status'], 'not_sent')
        factory.return_value.send_message.assert_not_called()

    def test_configuration_permissions(self):
        path = Path(self.temp.name) / 'config.json'
        path.write_text(json.dumps({'email': 'me@yahoo.com', 'app_password': 'test'}))
        path.chmod(0o644)
        with patch.dict(os.environ, YAHOO_MUSE_CONFIG=str(path)):
            with self.assertRaisesRegex(MailError, 'private'):
                Config.load()
            path.chmod(0o600)
            self.assertEqual(Config.load().email, 'me@yahoo.com')


if __name__ == '__main__':
    unittest.main()
