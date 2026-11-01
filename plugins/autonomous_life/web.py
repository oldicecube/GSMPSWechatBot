"""Read-only public HTTPS research. DNS pinned, no proxy/auth/local network."""
import http.client
import ipaddress
import re
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin, quote
import xml.etree.ElementTree as ET


def read(url, redirects=0, *, deadline=None):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 for c in url):
        raise ValueError('invalid public URL')
    deadline = deadline or time.monotonic() + 16
    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError('research time limit')
        return min(8, budget)
    target = urlsplit(url)
    if target.scheme != 'https' or not target.hostname or target.username or target.password or target.port not in (443, None):
        raise ValueError('public HTTPS only')
    ips = list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(target.hostname, 443, type=socket.SOCK_STREAM)))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError('non-public address denied')
    conn = http.client.HTTPSConnection(target.hostname, timeout=remaining(), context=ssl.create_default_context())
    # Pin the validated address while preserving TLS hostname verification/SNI.
    def connect():
        conn.sock = conn._context.wrap_socket(socket.create_connection((ips[0], 443), timeout=remaining()), server_hostname=target.hostname)
    conn.connect = connect
    try:
        path = quote(target.path or '/', safe="/%:@!$&'()*+,;=-._~")
        query = quote(target.query, safe="/?%:@!$&'()*+,;=-._~[]")
        conn.request('GET', path + ('?' + query if query else ''),
                     headers={'User-Agent': 'MaiBot-Research/1.0 (read-only)', 'Accept-Encoding': 'identity'})
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            if redirects >= 2:
                raise ValueError('redirect limit')
            location = response.getheader('Location', '')
            response.close()
            conn.close()
            return read(urljoin(url, location), redirects + 1, deadline=deadline)
        if response.status != 200:
            raise ValueError('HTTP ' + str(response.status))
        content_type = response.getheader('Content-Type', '')
        if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise ValueError('compressed response denied')
        if not any(t in content_type.lower() for t in ('text/', 'json', 'xml')):
            raise ValueError('non-text resource denied')
        # Short reads recheck the total deadline, including a slow streaming server.
        chunks, size = [], 0
        while size <= 524288:
            if conn.sock:
                conn.sock.settimeout(remaining())
            else:
                remaining()
            block = response.read1(min(16384, 524289-size))
            if not block:
                break
            chunks.append(block)
            size += len(block)
        encoding = response.headers.get_content_charset() or 'utf-8'
        try:
            text = b''.join(chunks)[:524288].decode(encoding, errors='replace')
        except LookupError:
            text = b''.join(chunks)[:524288].decode('utf-8', errors='replace')
        return dict(url=url, text=text, content_type=content_type, source_truncated=size > 524288)
    finally:
        conn.close()


class PageText(HTMLParser):
    """Extract readable text without executing page scripts or fetching subresources."""
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.title, self.links = base, [], [], []
        self.link_chars = 0
        self.hidden, self.in_title = [], False

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript', 'template', 'svg'}:
            self.hidden.append(tag)
        if self.hidden:
            return
        self.in_title = self.in_title or tag == 'title'
        if tag in {'p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'pre', 'tr'}:
            self.parts.append('\n')
        href = dict(attrs).get('href') if tag == 'a' else None
        if href and len(self.links) < 15 and self.link_chars < 4000:
            link = urljoin(self.base, href)
            if link.startswith('https://') and link not in self.links and len(link) <= 1500:
                self.links.append(link)
                self.link_chars += len(link)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag == 'title':
            self.in_title = False
        if tag in {'p', 'div', 'li', 'pre', 'tr'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title.append(data)


def explore(query='', url='', *, offset=0, max_chars=10000, max_results=5):
    if url:
        result = read(url)
        if 'html' in result['content_type'].lower():
            page = PageText(result['url']); page.feed(result['text'])
            text = '\n'.join(re.sub(r'[ \t\r\f\v]+', ' ', line).strip() for line in ''.join(page.parts).splitlines())
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            result.update(title=''.join(page.title).strip()[:300], links=page.links)
        else:
            text = result['text']
        result.update(text=text[offset:offset+max_chars], total_chars=len(text), offset=offset,
                      next_offset=offset+max_chars if offset+max_chars < len(text) else None,
                      truncated=result['source_truncated'] or offset+max_chars < len(text))
        return result
    if not 0 < len(query.strip()) <= 160:
        raise ValueError('query must be 1..160 characters')
    deadline = time.monotonic() + 16
    def search(term):
        last = None
        for host in ('www.bing.com', 'cn.bing.com'):
            try:
                result = read('https://' + host + '/search?format=rss&q=' + quote(term), deadline=deadline)
                break
            except (ValueError, OSError, http.client.HTTPException) as exc:
                last = exc
        else:
            raise ValueError('search unavailable; try a known primary source URL') from last
        if '<!DOCTYPE' in result['text'].upper() or '<!ENTITY' in result['text'].upper():
            raise ValueError('DTD denied')
        try:
            root = ET.fromstring(result['text'])
        except ET.ParseError as exc:
            raise ValueError('invalid search response') from exc
        rows = []
        for item in root.findall('.//item')[:20]:
            link = item.findtext('link') or ''
            try:
                parsed = urlsplit(link)
                if (parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password
                        or len(link) > 2048 or any(ord(c) < 33 for c in link)):
                    continue
            except ValueError:
                continue
            rows.append(dict(title=(item.findtext('title') or '')[:240], url=link,
                             excerpt=(item.findtext('description') or '')[:800]))
        return rows
    # Bing can latch onto a year and return calendars for a technical query.
    # One bounded retry drops only date qualifiers; unrelated snippets are not evidence.
    topic_query = re.sub(r'\b(?:19|20)\d{2}(?:年(?:\d{1,2}月)?|[-/]\d{1,2})?\b', ' ', query)
    topic_query = re.sub(r'(?:19|20)\d{2}年(?:\d{1,2}月)?', ' ', topic_query)
    terms = [w.lower() for w in re.findall(r'[A-Za-z][A-Za-z0-9.+-]{2,}|[\u4e00-\u9fff]{2,}', topic_query)
             if w.lower() not in {'the', 'and', 'for', 'with', 'from', 'latest', 'trending'}]
    def relevant(row):
        corpus = (row['title'] + ' ' + row['excerpt'] + ' ' + row['url']).lower()
        return not terms or any(word in corpus for word in terms)
    used = query
    rows = [r for r in search(used) if relevant(r)]
    if not rows and topic_query.strip() and topic_query.strip() != query.strip():
        used = re.sub(r'\s+', ' ', topic_query).strip()
        rows = [r for r in search(used) if relevant(r)]
    # Keep more specific matches ahead of generic pages matching only one word.
    rows.sort(key=lambda row: sum(word in (row['title']+' '+row['excerpt']+' '+row['url']).lower() for word in terms), reverse=True)
    return dict(query=query, search_query=used, results=rows[:max_results],
                note='Search excerpts only; no claim of current popularity, reading a full page/video. '
                     + ('No relevant results; change topic words or read a primary source.' if not rows else 'Verify relevance and dates.'))
