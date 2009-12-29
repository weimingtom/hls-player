import sys
import urlparse
import os.path

from twisted.internet import reactor
from twisted.web import client


def to_dict(l):
    i = (f.split('=') for f in l.split(','))
    d = dict((k.strip(), v.strip()) for (k,v) in i)
    return d

class M3U8(object):

    def __init__(self, url=None):
        self.url = url

        self._playlists = [] # main list of programs & bandwidth
        self._files = {} # the current program playlist
        self._first_sequence = 1 # the first sequence to start fetching
        self._last_sequence = None # the last sequence, to compute reload delay
        self._reload_delay = None # the initial reload delay
        self._update_tries = None # the number consecutive reload tries
        self._last_content = None
        self.endlist = False # wether the list ended and should not be refreshed

    def get_playlist(self, program_id=None, bandwidth=None):
        if len(self._playlists):
            return self._playlists[0]['uri']
        return None

    def reload_delay(self):
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

    def files(self):
        if len(self._files) == 0:
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
            elif l.startswith('#EXTINF'):
                v = l[8:].split(',')
                d = dict(file=self._lines.next(),
                         title=v[1].strip(),
                         duration=int(v[0]),
                         sequence=i)
                self._set_file(i, d)
                i += 1
                if i > self._last_sequence:
                    self._last_sequence = i
            elif l.startswith('#EXT-X-ENDLIST'):
                self.endlist = True
            elif len(l.strip()) != 0:
                print l

        if not self.get_playlist() and not self.target_duration:
            raise

        return True

    def _add_playlist(self, d):
        self._playlists.append(d)

    def _set_file(self, sequence, d):
        if sequence < self._first_sequence:
            self._first_sequence = sequence
        self._files[sequence] = d

    def __repr__(self):
        return "M3U8 %r %r" % (self._playlists, self._files)

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

    def _download_next_file(self):
        f = self._files.next()
        l = urlparse.urljoin(self.playlist.url, f['file'])
        path = os.path.join('/tmp/', f['file'])
        d = self._download_page(l, path)
        d.addCallback(self._got_file, l, path, f['duration'])

    def _got_file(self, _, l, path, duration):
        print "got " + l + " in " + path
        self._next_download = reactor.callLater(duration, self._download_next_file)

    def _get_files(self, files):
        self._files = files
        self._download_next_file()

    def _refresh_playlist(self, pl):
        self._get_page(pl.url).addCallback(self._got_playlist, pl.url, pl)

    def _got_playlist(self, content, url, pl):
        if not pl:
            pl = M3U8(url)

        if not pl.update(content):
            reactor.callLater(pl.reload_delay(), self._refresh, pl)
            return

        # if we got a program playlist, save it
        l = pl.get_playlist(1, 200000)
        if not self.program and l:
            self.program = pl
            l = urlparse.urljoin(self.url, l)
            self._get_page(l).addCallback(self._got_playlist, l, None)
        else:
            self.playlist = pl
            self._get_files(pl.files())
            if not pl.endlist:
                reactor.callLater(pl.reload_delay(), self._refresh, pl)

    def start(self):
        self._get_page(self.url).addCallback(self._got_playlist, self.url, None)

    def stop(self):
        pass

if __name__ == '__main__':

    player = HLSPlayer(sys.argv[1])
    player.start()
    reactor.run()
