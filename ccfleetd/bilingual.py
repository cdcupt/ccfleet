"""Separate authored English/Chinese pages; no mixed display or browser preference."""
from __future__ import annotations

import base64
import hashlib
import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

CURRENT_PAGES = frozenset({
    '/', '/slots', '/account', '/account/available', '/privacy', '/status', '/docs', '/docs/guide',
    '/docs/how-it-works', '/docs/terms', '/docs/library', '/docs/library/',
    '/docs/library/docs/index.html', '/docs/library/docs/public-gateway.html',
    '/docs/library/docs/devin.html',
    '/docs/library/docs/platform-comparison.html',
})
SLOT_ACTIONS = frozenset({'release', 'signin', 'switch', 'code', 'cancel', 'cli',
                        'cli-revoke', 'update', 'stable', 'rename', 'quota'})
POST_PAGES = frozenset({'/account/claim', '/account/outage-emails', '/auth/signout',
                       '/account/gateway/create', '/account/gateway/update',
                       '/account/gateway/revoke'})
VOID = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
                  'meta', 'param', 'source', 'track', 'wbr'})
TITLES_ZH = {'/': 'CC Fleet', '/docs': 'CC Fleet', '/slots': '槽位与价格', '/account': '我的槽位',
             '/account/available': '可用槽位',
             '/privacy': '隐私说明', '/status': '服务状态', '/docs/guide': '使用指南',
             '/docs/how-it-works': '工作原理', '/docs/terms': '服务条款',
             '/docs/library': '文档', '/docs/library/': '文档',
             '/docs/library/docs/index.html': '文档',
             '/docs/library/docs/public-gateway.html': 'HTTPS 接入指南',
             '/docs/library/docs/devin.html': '为什么评估 Devin',
             '/docs/library/docs/platform-comparison.html': 'CC Fleet 与 CC Host 对比',
             '/account/gateway/create': '连接密钥'}


def bi(en_html: str, zh_html: str, *, inline: bool = False) -> str:
    """Pair trusted authoring HTML; the HTTP boundary projects one language."""
    wrapper, tag = ('bi-inline', 'span') if inline else ('bi', 'div')
    return (f'<{tag} class="{wrapper}"><{tag} class="copy-en" lang="en">{en_html}</{tag}>'
            f'<{tag} class="copy-zh" lang="zh-Hans">{zh_html}</{tag}></{tag}>')


def label(en_text: str, zh_text: str, *, inline: bool = False) -> str:
    return bi(escape(en_text), escape(zh_text), inline=inline)


def _page(path: str) -> bool:
    return path in CURRENT_PAGES or (path.rstrip('/') in CURRENT_PAGES and path != '//')


def _form(path: str) -> bool:
    if path in POST_PAGES:
        return True
    match = re.fullmatch(r'/account/slots/([A-Za-z0-9_.:-]+)/([a-z-]+)', path)
    return bool(match and match[1] not in {'.', '..'} and match[2] in SLOT_ACTIONS)


def route_target(target: str, method: str = 'GET'):
    """Canonicalize only explicit, literal UI aliases, never model/API/admin paths."""
    try:
        parts = urlsplit(target)
    except (TypeError, ValueError):
        return None
    path, language, localized = parts.path, 'en', False
    for candidate in ('en', 'zh'):
        if path == '/' + candidate or path.startswith('/' + candidate + '/'):
            path = path[len(candidate) + 1:] or '/'
            language, localized = candidate, True
            break
    if localized:
        if '%' in path or '\\' in path or '//' in path or any(
                part in {'.', '..'} for part in path.split('/')):
            return None
        allowed = (_page(path) or path == '/auth/google/start') if method in ('GET', 'HEAD') \
            else _form(path) if method == 'POST' else False
        if not allowed:
            return None
    canonical = urlunsplit(('', '', path, parts.query, parts.fragment))
    return canonical, language, localized


def localized_url(url: str, language: str, canonical_target: str = '/',
                  public_origin: str = '') -> str:
    """Rewrite only known same-site UI links; leave providers and callbacks untouched."""
    language = 'zh' if language == 'zh' else 'en'
    try:
        original = urlsplit(url)
        origin = urlsplit(public_origin)
        if original.scheme or original.netloc:
            if (original.scheme != origin.scheme or original.netloc.lower() != origin.netloc.lower()
                    or not origin.netloc):
                return url
            resolved = original
        else:
            if url.startswith('//') or '\\' in url:
                return url
            resolved = urlsplit(urljoin(canonical_target, url))
        routed = route_target(urlunsplit(('', '', resolved.path, resolved.query,
                                         resolved.fragment)))
        if routed is None:
            return url
        path = urlsplit(routed[0]).path
        if not (_page(path) or _form(path) or path == '/auth/google/start'):
            return url
        query = [(key, value) for key, value in parse_qsl(resolved.query, keep_blank_values=True)
                 if key != 'lang']
        if path == '/auth/google/start':
            query = [(key, localized_url(value, language, public_origin=public_origin)
                      if key == 'next' else value) for key, value in query]
        selected = '/' + language + ('/' if path == '/' else path)
        return urlunsplit((resolved.scheme, resolved.netloc, selected, urlencode(query),
                           resolved.fragment))
    except (TypeError, ValueError):
        return url


CONTROLS = (
    '<nav class="language-control" aria-label="Language" data-language-links>'
    '<a href="/en/" data-language-link="en" lang="en">English</a>'
    '<a href="/zh/" data-language-link="zh" lang="zh-Hans">中文</a></nav>'
)
CSS = """
.bi>.copy-en,.bi>.copy-zh{display:block}
.bi-inline>.copy-en,.bi-inline>.copy-zh{display:inline}
body[data-language="en"] .copy-zh,body[data-language="zh"] .copy-en{display:none!important}
.copy-zh{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
letter-spacing:0;line-height:1.8}
.language-control{display:inline-flex;align-items:center;gap:3px;padding:3px;
border:1px solid var(--rule,#d8dee8);border-radius:10px;background:var(--panel,#fff);
flex-shrink:0;max-width:100%}
.language-control a{display:inline-block;border-radius:7px;padding:6px 9px;
color:var(--muted,#667085);font:600 12px/1.4 system-ui;text-decoration:none;white-space:nowrap}
.language-control a[aria-current="page"]{background:var(--acc-soft,#f0efff);
color:var(--acc,#4932c5)}
.language-control a:focus-visible{outline:2px solid var(--acc,#4932c5);outline-offset:2px}
@media print{.language-control{display:none}}
"""
# Navigation works without JavaScript. This only preserves the current chapter anchor.
SCRIPT = """(()=>{'use strict';
document.querySelectorAll('a[data-language-link]').forEach(link=>{
  link.addEventListener('click',()=>{
    const target=new URL(link.href,location.href);
    if(target.origin===location.origin){target.hash=location.hash;link.href=target.href;}
  });
});
})();"""
SCRIPT_TAG = f'<script>{SCRIPT}</script>'
SCRIPT_CSP = "'sha256-" + base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode() + "'"


def _slot_search_query(query: str) -> str:
    """Retain only bounded browse controls, never arbitrary/auth request values."""
    try:
        pairs = parse_qsl(query, keep_blank_values=False, max_num_fields=16)
    except ValueError:
        return ''
    result = {}
    for key, raw in pairs:
        if key not in {'q', 'brand', 'type', 'page'} or key in result:
            continue
        value = raw.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            continue
        if key == 'q':
            value = value[:80]
        elif key == 'brand' and len(value) > 48:
            continue
        elif key == 'type' and value not in {'isp', 'residential', 'datacenter'}:
            continue
        elif key == 'page' and (not value.isascii() or not value.isdigit()
                               or not 0 < len(value) <= 6 or int(value) < 1):
            continue
        result[key] = value
    return urlencode(result)


class _Projection(HTMLParser):
    def __init__(self, language: str, target: str, public_origin: str):
        super().__init__(convert_charrefs=False)
        self.language = 'zh' if language == 'zh' else 'en'
        self.target, self.public_origin = target, public_origin
        self.frames = []
        self.output = []

    @property
    def suppressed(self):
        return bool(self.frames and self.frames[-1][1])

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, True)

    def _start(self, tag, attrs, closed):
        attrs = dict(attrs)
        classes = set((attrs.get('class') or '').split())
        hidden = self.suppressed or ('copy-zh' in classes if self.language == 'en'
                                     else 'copy-en' in classes)
        hidden = hidden or bool(classes & {'reference-catalog', 'earlier-reference',
                                           'host-history-note'})
        override = None
        if tag == 'option' and 'data-copy-' + self.language in attrs:
            override = attrs['data-copy-' + self.language]
        if tag == 'title' and self.language == 'zh':
            path = urlsplit(self.target).path.rstrip('/') or '/'
            words = attrs.get('data-title-zh') or TITLES_ZH.get(path)
            if _form(path) and '/slots/' in path:
                words = '连接电脑' if path.endswith('/cli') else '我的槽位'
            if words:
                override = words + ' · CC Fleet' if words != 'CC Fleet' else words
        if not hidden:
            if tag == 'html':
                attrs['lang'] = 'zh-Hans' if self.language == 'zh' else 'en'
            if tag == 'body':
                attrs['data-language'] = self.language
            if 'data-language-links' in attrs:
                attrs['aria-label'] = '语言' if self.language == 'zh' else 'Language'
            if 'data-language-link' in attrs:
                choice = attrs['data-language-link']
                switch_target = ('/account#gateway' if _form(urlsplit(self.target).path)
                                 else self.target)
                switch_parts = urlsplit(switch_target)
                search = ''
                if switch_parts.path in {'/slots', '/account/available'}:
                    search = _slot_search_query(switch_parts.query)
                switch_target = urlunsplit(('', '', switch_parts.path, search,
                                             switch_parts.fragment))
                attrs['href'] = localized_url(switch_target, choice, self.target,
                                              self.public_origin)
                if choice == self.language:
                    attrs['aria-current'] = 'page'
                else:
                    attrs.pop('aria-current', None)
            else:
                for attribute in ('href', 'action'):
                    if attribute in attrs:
                        value = localized_url(attrs[attribute], self.language,
                                              self.target, self.public_origin)
                        if self.language == 'en':
                            parts = urlsplit(value)
                            origin = urlsplit(self.public_origin)
                            same_site = (not parts.scheme and not parts.netloc or
                                         bool(origin.netloc) and parts.scheme == origin.scheme
                                         and parts.netloc.lower() == origin.netloc.lower())
                            if same_site and (parts.path == '/en' or parts.path.startswith('/en/')):
                                value = urlunsplit((parts.scheme, parts.netloc,
                                                    parts.path[3:] or '/',
                                                    parts.query, parts.fragment))
                        attrs[attribute] = value
            for attribute in ('placeholder', 'aria-label', 'title', 'data-label'):
                name = ('label' if attribute == 'data-label' else attribute)
                source = 'data-' + name + '-' + self.language
                if source in attrs:
                    attrs[attribute] = attrs[source]
            attrs = {name: value for name, value in attrs.items()
                     if not re.fullmatch(
                         r'data-(copy|placeholder|aria-label|title|label)-(en|zh)', name)}
            encoded = ''.join(' ' + name if value is None else
                              ' ' + name + '="' + escape(value, quote=True) + '"'
                              for name, value in attrs.items())
            self.output.append('<' + tag + encoded + ('/>' if closed else '>'))
            if override is not None:
                self.output.append(escape(override))
        if not closed and tag not in VOID:
            self.frames.append((tag, hidden, override))

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.frames) - 1, -1, -1)
                      if self.frames[i][0] == tag), None)
        if index is None:
            return
        hidden = self.frames[index][1]
        del self.frames[index:]
        if not hidden:
            self.output.append('</' + tag + '>')

    def handle_data(self, data):
        if not self.suppressed and not (self.frames and self.frames[-1][2] is not None):
            self.output.append(data)

    def handle_entityref(self, name):
        if not self.suppressed and not (self.frames and self.frames[-1][2] is not None):
            self.output.append('&' + name + ';')

    def handle_charref(self, name):
        if not self.suppressed and not (self.frames and self.frames[-1][2] is not None):
            self.output.append('&#' + name + ';')

    def handle_decl(self, decl):
        if not self.suppressed:
            self.output.append('<!' + decl + '>')

    def handle_comment(self, data):
        if not self.suppressed:
            self.output.append('<!--' + data + '-->')


def project_html(html: str, language: str = 'en', target: str = '/',
                 public_origin: str = '') -> str:
    """Project trusted page markup without changing scripts, keys or form values."""
    parser = _Projection(language, target, public_origin)
    parser.feed(html)
    parser.close()
    return ''.join(parser.output)
