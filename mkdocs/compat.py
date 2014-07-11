# coding: utf-8
"""Python 2/3 compatibility module."""
import sys

PY2 = int(sys.version[0]) == 2

if PY2:
    from urlparse import urljoin, urlparse, urlunparse
    import urllib
    urljoin = urljoin
    urlparse = urlparse
    urlunparse = urlunparse
    urlunquote = urllib.unquote

    import SimpleHTTPServer as httpserver
    httpserver = httpserver
    import SocketServer
    socketserver = SocketServer

    text_type = unicode
    binary_type = str
    string_types = (str, unicode)
    unicode = unicode
    basestring = basestring
else:  # PY3
    from urllib.parse import urljoin, urlparse, urlunparse, unquote
    urljoin = urljoin
    urlparse = urlparse
    urlunparse = urlunparse
    urlunquote = unquote

    import http.server as httpserver
    httpserver = httpserver
    import socketserver
    socketserver = socketserver

    text_type = str
    binary_type = bytes
    string_types = (str,)
    unicode = str
    basestring = (str, bytes)
