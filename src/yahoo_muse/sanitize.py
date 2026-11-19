"""Sanitization of untrusted mailbox content.

Everything that comes out of a mailbox (subjects, sender names, bodies,
attachment names) is attacker-controlled. This module turns it into inert
plain text before it is handed to the language model:

* Invisible / control characters (zero-width, bidi overrides, Unicode "tag"
  characters, variation selectors) are removed - they are used for ASCII
  smuggling of hidden instructions and for covert data channels.
* HTML bodies are converted to plain text; hidden elements (display:none,
  font-size:0, ...) that are a typical vehicle for prompt injection are
  removed and reported. Scripts, styles and other active content are dropped.
* Markdown images and links are neutralised and URLs are defanged, so that
  content copied verbatim into a model answer cannot make the client fetch
  an attacker URL (the exfiltration channel of CVE-2025-32711 "EchoLeak").
* Tool-call-like markup (``<function_calls>``, ``[TOOL_CALL]``, ...), which
  an attacker can use to fake a tool invocation inside an email, is
  neutralised.

Modeled on the approach in carstenkiefer/imap-mcp (MIT), reimplemented here
in dependency-free stdlib Python.
"""

import re
from html.parser import HTMLParser

# --- invisible characters ---------------------------------------------------

# C0/C1 control characters except TAB, LF, CR.
_CONTROL = '\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f'

# Soft hyphen, combining grapheme joiner, Hangul fillers, Mongolian vowel
# separators, zero-width space/joiners, LRM/RLM, bidi embeddings/overrides/
# isolates, word joiner & invisible operators, BOM, interlinear annotation,
# variation selectors.
_INVISIBLE_BMP = (
    '\u00ad\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e'
    '\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffb'
)

# Unicode tag characters (U+E0000-U+E007F) and supplementary variation
# selectors (U+E0100-U+E01EF).
_INVISIBLE_ASTRAL = '\U000e0000-\U000e007f\U000e0100-\U000e01ef'

_INVISIBLE_RE = re.compile('[' + _CONTROL + _INVISIBLE_BMP + _INVISIBLE_ASTRAL + ']')


def strip_invisible(text):
    """Remove invisible and control characters.

    Returns (text, removed_count).
    """
    removed = [0]

    def drop(_match):
        removed[0] += 1
        return ''

    cleaned = _INVISIBLE_RE.sub(drop, str(text if text is not None else ''))
    return cleaned.replace('\r\n', '\n').replace('\r', '\n'), removed[0]


# --- URLs and markdown ------------------------------------------------------

_URL_RE = re.compile(r'\b((?:https?|ftp|ftps|wss?|file|sftp)://|www\.)[^\s<>"\'`]+', re.IGNORECASE)
_DANGEROUS_SCHEME_RE = re.compile(r'\b(?:data|javascript|vbscript|blob):[^\s<>"\'`]*', re.IGNORECASE)


def _defang_url(url):
    lowered = url.lower()
    if lowered.startswith('http'):
        url = ('HXXP' if url[0] == 'H' else 'hxxp') + url[4:]
    elif lowered.startswith('ftp'):
        url = 'fxp' + url[3:]
    elif lowered.startswith('ws'):
        url = 'wx' + url[2:]
    elif lowered.startswith('file'):
        url = 'fxle' + url[4:]
    url = url.replace('://', '[://]', 1)
    return url.replace('.', '[.]')


def neutralize_links(text, url_mode='defang'):
    """Neutralise markdown constructs that clients render as active content
    (images, links, reference definitions, raw HTML) and defang URLs.

    url_mode is 'defang' (default), 'remove', or 'keep' (not recommended).

    Returns (text, urls_count).
    """
    urls = [0]
    out = text

    # Markdown images: ![alt](target) / ![alt][ref]  -> [image: alt]
    out = re.sub(r'!\[([^\]\n]*)\]\s*(\([^)\n]*\)|\[[^\]\n]*\])',
                 lambda m: '[image: %s]' % (m.group(1).strip() or 'untitled'), out)

    # Inline markdown links: [label](target) -> label <target>
    out = re.sub(r'\[([^\]\n]*)\]\(([^)\n]*)\)',
                 lambda m: '%s <%s>' % (m.group(1), m.group(2).strip()), out)

    # Reference definitions at line start: [ref]: target -> (link reference ref: target)
    out = re.sub(r'^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*(\S+).*$',
                 lambda m: '(link reference %s: %s)' % (m.group(1), m.group(2)),
                 out, flags=re.MULTILINE)

    # data:, javascript: ... are never useful to a mail assistant.
    def _drop_dangerous(_match):
        urls[0] += 1
        return '[removed URI]'
    out = _DANGEROUS_SCHEME_RE.sub(_drop_dangerous, out)

    if url_mode != 'keep':
        def _handle_url(match):
            urls[0] += 1
            return '[URL removed]' if url_mode == 'remove' else _defang_url(match.group(0))
        out = _URL_RE.sub(_handle_url, out)
    else:
        _URL_RE.sub(lambda _m: urls[0].__iadd__(1) or '', out)

    # Raw HTML tags that might have survived (e.g. in text/plain parts) are
    # escaped so a markdown renderer shows them as text.
    def _escape_tag(match):
        inner = match.group(1)
        if re.match(r'^[^\s]+@[^\s]+$', inner):
            return match.group(0)  # e-mail address
        if re.match(r'^(hxxps?|fxp|wxs?|fxle)\b', inner, re.IGNORECASE) or '[://]' in inner:
            return match.group(0)  # our own defanged-URL notation
        return '\u2039%s\u203a' % inner
    out = re.sub(r'<(/?[a-zA-Z][^<>\n]*)>', _escape_tag, out)

    return out, urls[0]


# --- tool-call-like markup --------------------------------------------------

_TOOLCALL_RE = re.compile(
    r'</?(?:function_calls|invoke|antml:[a-zA-Z_]+|tool_calls?|tool_result|tool_use|'
    r'function|mcp|system|assistant|human)\b[^<>]*>',
    re.IGNORECASE,
)
_BRACKET_CALL_RE = re.compile(
    r'\[(?:TOOL_CALLS?|SYSTEM|INST|/INST|FUNCTION_CALL|ACTION)\]',
    re.IGNORECASE,
)


def neutralize_tool_calls(text):
    """Neutralise markup that tries to fake a tool invocation or a system
    message inside email content.

    Returns (text, removed_count).
    """
    removed = [0]

    def _drop(_match):
        removed[0] += 1
        return '[removed markup]'

    out = _TOOLCALL_RE.sub(_drop, text)
    out = _BRACKET_CALL_RE.sub(_drop, out)
    return out, removed[0]


# --- HTML -------------------------------------------------------------------

_HIDDEN_STYLE_RE = re.compile(
    '|'.join([
        r'display\s*:\s*none',
        r'visibility\s*:\s*(hidden|collapse)',
        r'opacity\s*:\s*0*(\.0+)?\s*(;|$|!)',
        r'font-size\s*:\s*0+(\.0+)?\s*(px|pt|em|rem|%)?\s*(;|$|!)',
        r'(max-)?height\s*:\s*0+(px)?\s*(;|$|!)[\s\S]*overflow\s*:\s*hidden',
        r'overflow\s*:\s*hidden[\s\S]*(max-)?height\s*:\s*0+(px)?\s*(;|$|!)',
        r'mso-hide\s*:\s*all',
        r'clip-path\s*:\s*inset\(\s*50%',
        r'text-indent\s*:\s*-\d{3,}',
        r'(left|top)\s*:\s*-\d{4,}',
    ]),
    re.IGNORECASE,
)

_DROP_TAGS = frozenset([
    'script', 'style', 'head', 'title', 'noscript', 'template', 'object',
    'embed', 'iframe', 'svg', 'math', 'meta', 'link', 'base',
])

_BLOCK_TAGS = frozenset([
    'p', 'div', 'section', 'article', 'header', 'footer', 'li', 'ul', 'ol',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr', 'table', 'blockquote', 'pre',
    'hr', 'br',
])


def _is_hidden(tag, attrs):
    attrs = dict(attrs)
    if 'hidden' in attrs:
        return True
    style = attrs.get('style', '')
    if style and _HIDDEN_STYLE_RE.search(style):
        return True
    if (attrs.get('width') == '0' or attrs.get('height') == '0') and tag != 'img':
        return True
    return False


class _SafeHTMLParser(HTMLParser):
    """Convert HTML to plain text, dropping active and hidden content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_removed = 0
        self.images = 0
        self._drop_depth = 0
        self._hidden_depth = 0
        self._anchor_href = None
        self._anchor_text = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._drop_depth:
            if tag in _DROP_TAGS:
                self._drop_depth += 1
            return
        if tag in _DROP_TAGS:
            self._drop_depth = 1
            return
        if self._hidden_depth:
            self._hidden_depth += 1
            return
        if _is_hidden(tag, attrs):
            self._hidden_depth = 1
            self._hidden_text = ''
            return
        if tag == 'img':
            self.images += 1
            alt = dict(attrs).get('alt', '').strip()
            self.parts.append('[image%s]' % (': ' + alt if alt else ''))
            return
        if tag == 'a':
            self._anchor_href = dict(attrs).get('href', '').strip()
            self._anchor_text = ''
            return
        if tag in _BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._drop_depth:
            if tag in _DROP_TAGS:
                self._drop_depth -= 1
            return
        if self._hidden_depth:
            self._hidden_depth -= 1
            if self._hidden_depth == 0 and getattr(self, '_hidden_text', '').strip():
                self.hidden_removed += 1
            return
        if tag == 'a' and self._anchor_href is not None:
            text = (self._anchor_text or '').strip()
            href = self._anchor_href
            if href and href != text:
                self.parts.append(' <%s>' % href)
            self._anchor_href = None
            self._anchor_text = None
            return
        if tag in _BLOCK_TAGS:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _DROP_TAGS and tag.lower() not in ('br', 'img', 'hr'):
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self._drop_depth:
            return
        if self._hidden_depth:
            self._hidden_text += data
            return
        if self._anchor_href is not None:
            self._anchor_text += data
        self.parts.append(data)

    def handle_comment(self, data):
        return

    def handle_decl(self, decl):
        return


def html_to_safe_text(html):
    """Convert HTML to plain text after removing hidden elements.

    Returns (text, hidden_removed, images).
    """
    parser = _SafeHTMLParser()
    parser.feed(str(html if html is not None else ''))
    parser.close()
    text = ''.join(parser.parts)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text, parser.hidden_removed, parser.images


# --- public API -------------------------------------------------------------


def sanitize_line(value, url_mode='defang', max_length=500):
    """Sanitize a short single-line field (subject, display name, file name)."""
    text, _ = strip_invisible(value)
    line = re.sub(r'\s+', ' ', text).strip()
    line, _ = neutralize_links(line, url_mode)
    line, _ = neutralize_tool_calls(line)
    if len(line) > max_length:
        line = line[:max_length] + '\u2026'
    return line


def sanitize_body(text, is_html=False, url_mode='defang'):
    """Sanitize an e-mail body.

    Returns (text, warnings) where warnings is a list of human-readable notes
    about what was removed or defanged. HTML bodies are converted to plain
    text first (hidden elements dropped); plain bodies are cleaned in place.
    """
    warnings = []
    body = str(text if text is not None else '')

    if is_html and body.strip():
        body, hidden_removed, images = html_to_safe_text(body)
        if hidden_removed:
            warnings.append(
                'Removed %d hidden HTML element(s) containing text '
                '(text invisible to humans is a typical prompt-injection vehicle).'
                % hidden_removed)
        if images:
            warnings.append('Removed %d image(s); images are never loaded.' % images)

    body, removed = strip_invisible(body)
    if removed:
        warnings.append(
            'Removed %d invisible/control character(s) (possible hidden instructions).'
            % removed)

    body, calls = neutralize_tool_calls(body)
    if calls:
        warnings.append(
            'Neutralised %d tool-call-like markup fragment(s) (possible fake tool invocation).'
            % calls)

    body, urls = neutralize_links(body, url_mode)
    if urls and url_mode == 'defang':
        warnings.append('Defanged %d URL(s) (hxxp\u2026[.]) - do not open or reconstruct them.' % urls)
    if urls and url_mode == 'remove':
        warnings.append('Removed %d URL(s).' % urls)

    body = re.sub(r'[ \t]+\n', '\n', body)
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    return body, warnings
