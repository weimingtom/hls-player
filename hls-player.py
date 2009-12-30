#!/usr/bin/env python

import sys
import urlparse
import optparse
import os.path

from twisted.internet import reactor
from twisted.web import client

if sys.version_info < (2, 4):
    raise ImportError("Cannot run with Python version < 2.4")

def to_dict(l):
    i = (f.split('=') for f in l.split(','))
    d = dict((k.strip(), v.strip()) for (k,v) in i)
    return d

def make_url(base_url, url):
    if urlparse.urlsplit(url).scheme != '':
        return url
    return urlparse.urljoin(base_url, url)

class M3U8(object):

    def __init__(self, url=None):
        self.url = url

        self._programs = [] # main list of programs & bandwidth
        self._files = {} # the current program playlist
        self._first_sequence = 1 # the first sequence to start fetching
        self._last_sequence = None # the last sequence, to compute reload delay
        self._reload_delay = None # the initial reload delay
        self._update_tries = None # the number consecutive reload tries
        self._last_content = None
        self.endlist = False # wether the list ended and should not be refreshed

    def has_programs(self):
        return len(self._programs) != 0

    def get_program_playlist(self, program_id=None, bandwidth=None):
        # return the (uri, dict) of the best matching playlist
        if not self.has_programs():
            raise
        return (self._programs[0]['uri'], self._programs[0])

    def reload_delay(self):
        # return the time between request updates, in seconds
        if self.endlist or not self._last_sequence:
            raise

        if self._update_tries == 0:
            ld = self._files[self._last_sequence]
            self._reload_delay = min(self.target_duration * 3, ld)
            return self._reload_delay
        elif self._update_tries == 1:
            return self._reload_delay * 0.5
        elif self._update_tries == 2:
            return self._reload_delay * 1.5
        else:
            return self._reload_delay * 3.0

    def has_files(self):
        return len(self._files) != 0

    def iter_files(self):
        # return an iter on the playlist media files
        if not self.has_files():
            return

        current = self._first_sequence
        while True:
            try:
                f = self._files[current]
            except:
                break
            current += 1
            yield f

    def update(self, content):
        # update this "constructed" playlist,
        # return wether it has actually been updated
        if self._last_content and content == self.last_content:
            self._update_tries += 1
            return False

        self._update_tries = 0
        self.last_content = content

        def get_line(c):
            for l in c.split('\n'):
                if l.startswith('#EXT'):
                    yield l
                elif l.startswith('#'):
                    pass
                else:
                    yield l
                
        self._lines = get_line(content)
        if not self._lines.next().startswith('#EXTM3U'):
            raise

        self.target_duration = None
        discontinuity = False
        i = 1
        for l in self._lines:
            if l.startswith('#EXT-X-STREAM-INF'):
                d = to_dict(l[18:])
                d['uri'] = self._lines.next()
                self._add_playlist(d)
            elif l.startswith('#EXT-X-TARGETDURATION'):
                self.target_duration = int(l[22:])
            elif l.startswith('#EXT-X-MEDIA-SEQUENCE'):
                self.media_sequence = int(l[22:])
                i = self.media_sequence
            elif l.startswith('EXT-X-DISCONTINUITY'):
                discontinuity = True
            elif l.startswith('EXT-X-PROGRAM-DATE-TIME'):
                print l
            elif l.startswith('#EXTINF'):
                v = l[8:].split(',')
                d = dict(file=self._lines.next(),
                         title=v[1].strip(),
                         duration=int(v[0]),
                         sequence=i,
                         discontinuity=discontinuity)
                discontinuity = False
                self._set_file(i, d)
                i += 1
                if i > self._last_sequence:
                    self._last_sequence = i
            elif l.startswith('#EXT-X-ENDLIST'):
                self.endlist = True
            elif len(l.strip()) != 0:
                print l

        if not self.has_programs() and not self.target_duration:
            raise

        return True

    def _add_playlist(self, d):
        self._programs.append(d)

    def _set_file(self, sequence, d):
        if sequence < self._first_sequence:
            self._first_sequence = sequence
        self._files[sequence] = d

    def __repr__(self):
        return "M3U8 %r %r" % (self._programs, self._files)

class HLSPlayer(object):

    def __init__(self, url):
        self.url = url
        self.program = None
        self.playlist = None
        self.cookies = {}

    def _get_page(self, url):
        return client.getPage(url, cookies=self.cookies)

    def _download_page(self, url, path):
        # client.downloadPage does not support cookies!
        d = self._get_page(url)
        f = open(path, 'w')
        d.addCallback(lambda x: f.write(x))
        d.addBoth(lambda _: f.close())
        return d

    def _download_file(self, f):
        l = make_url(self.playlist.url, f['file'])
        path = os.path.join('/tmp/', f['file'])
        d = self._download_page(l, path)
        d.addCallback(self._got_file, l, path, f)
        return d

    def _got_file(self, _, l, path, f):
        print "got " + l + " in " + path
        try:
            next = self._files.next()
            self._next_download = reactor.callLater(f['duration'], self._download_file, next)
        except StopIteration:
            pass

    def _get_files(self, files):
        self._files = files
        try:
            next = self._files.next()
            self._download_file(next)
        except StopIteration:
            pass

    def _got_playlist_content(self, content, pl):
        if not pl.update(content):
            # if the playlist cannout be loaded, start a reload timer
            reactor.callLater(pl.reload_delay(), self._reload_playlist, pl)
            return

        if pl.has_programs():
            # if we got a program playlist, save it and start a program
            self.program = pl
            (program_url, _) = pl.get_program_playlist(1, 200000)
            l = make_url(self.url, program_url)
            self._get_page(l).addCallback(self._got_playlist_content, M3U8(l))
        elif pl.has_files():
            # we got sequence playlist, start reloading it regularly
            self.playlist = pl
            self._get_files(pl.iter_files())
            if not pl.endlist:
                reactor.callLater(pl.reload_delay(), self._reload_playlist, pl)
        else:
            raise

    def _reload_playlist(self, pl):
        self._get_page(pl.url).addCallback(self._got_playlist_content, pl)

    def start(self):
        self._reload_playlist(M3U8(self.url))

    def stop(self):
        pass

def main():
    parser = optparse.OptionParser(usage='%prog [options] url...', version="%prog")

    parser.add_option('-v', '--verbose', action="store_true",
                      dest='verbose', default=False,
                      help='print some debugging (default: %default)')
    parser.add_option('-d', '--download', action="store_true",
                      dest='download', default=False,
                      help='only download files (default: %default)')
    parser.add_option('-n', '--number', action="store",
                      dest='n', default=1, type="int",
                      help='number of player to start (default: %default)')

    options, args = parser.parse_args()

    if len(args) == 0:
        parser.print_help()
        sys.exit(1)
    
    for url in args:
        for l in range(options.n):
            player = HLSPlayer(url)
            player.start()

    reactor.run()


if __name__ == '__main__':
    sys.exit(main())
