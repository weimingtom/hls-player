# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4
#
# Copyright (C) 2009-2010 Fluendo, S.L. (www.fluendo.com).
# Copyright (C) 2009-2010 Marc-Andre Lureau <marcandre.lureau@gmail.com>

# This file may be distributed and/or modified under the terms of
# the GNU General Public License version 2 as published by
# the Free Software Foundation.
# This file is distributed without any warranty; without even the implied
# warranty of merchantability or fitness for a particular purpose.
# See "LICENSE" in the source distribution for more information.

import os
import urlparse


def make_url(base_url, url):
    if urlparse.urlsplit(url).scheme == '':
        url = urlparse.urljoin(base_url, url)
    if 'HLS_PLAYER_SHIFT_PORT' in os.environ.keys():
        shift = int(os.environ['HLS_PLAYER_SHIFT_PORT'])
        p = urlparse.urlparse(url)
        loc = p.netloc
        if loc.find(":") != -1:
            loc, port = loc.split(':')
            port = int(port) + shift
            loc = loc + ":" + str(port)
        elif p.scheme == "http":
            port = 80 + shift
            loc = loc + ":" + str(shift)
        p = urlparse.ParseResult(scheme=p.scheme,
                                 netloc=loc,
                                 path=p.path,
                                 params=p.params,
                                 query=p.query,
                                 fragment=p.fragment)
        url = urlparse.urlunparse(p)
    return url
