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

import logging


class M3U8(object):

    def __init__(self, url=None):
        self.url = url

        self._programs = [] # main list of programs & bandwidth
        self._files = {} # the current program playlist
        self._first_sequence = None # the first sequence to start fetching
        self._last_sequence = None # the last sequence, to compute reload delay
        self._reload_delay = None # the initial reload delay
        self._update_tries = None # the number consecutive reload tries
        self._last_content = None
        self._endlist = False # wether the list ended and should not be refreshed

    def endlist(self):
        return self._endlist

    def has_programs(self):
        return len(self._programs) != 0

    def get_program_playlist(self, program_id=None, bitrate=None):
        # return the (uri, dict) of the best matching playlist
        if not self.has_programs():
            raise
        _, best = min((abs(int(x['BANDWIDTH']) - bitrate), x)
                for x in self._programs)
        return best['uri'], best

    def reload_delay(self):
        # return the time between request updates, in seconds
        if self._endlist or not self._last_sequence:
            raise

        if self._update_tries == 0:
            ld = self._files[self._last_sequence]['duration']
            self._reload_delay = min(self.target_duration * 3, ld)
            d = self._reload_delay
        elif self._update_tries == 1:
            d = self._reload_delay * 0.5
        elif self._update_tries == 2:
            d = self._reload_delay * 1.5
        else:
            d = self._reload_delay * 3.0

        logging.debug('Reload delay is %r' % d)
        return int(d)

    def has_files(self):
        return len(self._files) != 0

    def iter_files(self):
        # return an iter on the playlist media files
        if not self.has_files():
            return

        if not self._endlist:
            current = max(self._first_sequence, self._last_sequence - 3)
        else:
            # treat differently on-demand playlists?
            current = self._first_sequence

        while True:
            try:
                f = self._files[current]
                current += 1
                yield f
                if (f.has_key('endlist')):
                    break
            except:
                yield None

    def update(self, content):
        # update this "constructed" playlist,
        # return wether it has actually been updated
        if self._last_content and content == self._last_content:
            logging.info("Content didn't change")
            self._update_tries += 1
            return False

        self._update_tries = 0
        self._last_content = content

        def get_lines_iter(c):
            c = c.decode("utf-8-sig")
            for l in c.split('\n'):
                if l.startswith('#EXT'):
                    yield l
                elif l.startswith('#'):
                    pass
                else:
                    yield l

        self._lines = get_lines_iter(content)
        first_line = self._lines.next()
        if not first_line.startswith('#EXTM3U'):
            logging.error('Invalid first line: %r' % first_line)
            raise

        self.target_duration = None
        discontinuity = False
        allow_cache = None
        i = 0
        new_files = []
        for l in self._lines:
            if l.startswith('#EXT-X-STREAM-INF'):
                def to_dict(l):
                    i = (f.split('=') for f in l.split(','))
                    d = dict((k.strip(), v.strip()) for (k,v) in i)
                    return d
                d = to_dict(l[18:])
                d['uri'] = self._lines.next()
                self._add_playlist(d)
            elif l.startswith('#EXT-X-TARGETDURATION'):
                self.target_duration = int(l[22:])
            elif l.startswith('#EXT-X-MEDIA-SEQUENCE'):
                self.media_sequence = int(l[22:])
                i = self.media_sequence
            elif l.startswith('#EXT-X-DISCONTINUITY'):
                discontinuity = True
            elif l.startswith('#EXT-X-PROGRAM-DATE-TIME'):
                print l
            elif l.startswith('#EXT-X-ALLOW-CACHE'):
                allow_cache = l[19:]
            elif l.startswith('#EXTINF'):
                v = l[8:].split(',')
                d = dict(file=self._lines.next().strip(),
                         title=v[1].strip(),
                         duration=int(v[0]),
                         sequence=i,
                         discontinuity=discontinuity,
                         allow_cache=allow_cache)
                discontinuity = False
                i += 1
                new = self._set_file(i, d)
                if i > self._last_sequence:
                    self._last_sequence = i
                if new:
                    new_files.append(d)
            elif l.startswith('#EXT-X-ENDLIST'):
                if i > 0:
                    self._files[i]['endlist'] = True
                self._endlist = True
            elif len(l.strip()) != 0:
                print l

        if not self.has_programs() and not self.target_duration:
            logging.error("Invalid HLS stream: no programs & no duration")
            raise
        if len(new_files):
            logging.debug("got new files in playlist: %r", new_files)

        return True

    def _add_playlist(self, d):
        self._programs.append(d)

    def _set_file(self, sequence, d):
        new = False
        if not self._files.has_key(sequence):
            new = True
        if not self._first_sequence:
            self._first_sequence = sequence
        elif sequence < self._first_sequence:
            self._first_sequence = sequence
        self._files[sequence] = d
        return new

    def __repr__(self):
        return "M3U8 %r %r" % (self._programs, self._files)
