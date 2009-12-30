#!/usr/bin/env python

import sys
import urlparse
import optparse
import os.path
import logging
import tempfile

import pygtk, gtk, gobject
import pygst
pygst.require("0.10")
import gst

from twisted.internet import reactor
from twisted.web import client
from twisted.internet import gtk2reactor

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
        self._first_sequence = None # the first sequence to start fetching
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
            d = self._reload_delay
        elif self._update_tries == 1:
            d = self._reload_delay * 0.5
        elif self._update_tries == 2:
            d = self._reload_delay * 1.5
        else:
            d = self._reload_delay * 3.0

        logging.debug('Reload delay is %r' % d)
        return d

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
            logging.debug('next file is %r' % f)
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
                if i > self._last_sequence:
                    self._last_sequence = i
                i += 1
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
        if not self._first_sequence:
            self._first_sequence = sequence
        elif sequence < self._first_sequence:
            self._first_sequence = sequence
        self._files[sequence] = d

    def __repr__(self):
        return "M3U8 %r %r" % (self._programs, self._files)

class HLSFetcher(object):

    def __init__(self, url, path=None, player=None):
        self.url = url
        self.path = path
        if not self.path:
            self.path = tempfile.mkdtemp()
        self.player = player

        self.program = None
        self.playlist = None
        self._cookies = {}

        self._files = None # the iter of the playlist files, if downloading
        self._next_download = None # the delayed download defer, if any

    def _get_page(self, url):
        return client.getPage(url, cookies=self._cookies)

    def _download_page(self, url, path):
        # client.downloadPage does not support cookies!
        d = self._get_page(url)
        f = open(path, 'w')
        d.addCallback(lambda x: f.write(x))
        d.addBoth(lambda _: f.close())
        d.addCallback(lambda _: path)
        d.addCallback(self.player.play)
        return d

    def _download_file(self, f):
        l = make_url(self.playlist.url, f['file'])
        name = urlparse.urlparse(f['file']).path.split('/')[-1]
        path = os.path.join(self.path, name)
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
        logging.debug(content)
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
            # we got sequence playlist, start reloading it regularly, and get files
            self.playlist = pl
            if not self._files:
                self._get_files(pl.iter_files())
            if not pl.endlist:
                reactor.callLater(pl.reload_delay(), self._reload_playlist, pl)
        else:
            raise

    def _reload_playlist(self, pl):
        logging.debug('fetching %r' % pl.url)
        self._get_page(pl.url).addCallback(self._got_playlist_content, pl)

    def start(self):
        self._reload_playlist(M3U8(self.url))

    def stop(self):
        pass

class GSTPlayer:
    
    def __init__(self):
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        self.window.set_title("Video-Player")
        self.window.set_default_size(500, 400)
        self.window.set_type_hint(gtk.gdk.WINDOW_TYPE_HINT_DIALOG)
        self.window.connect('delete-event', lambda _: reactor.stop())
        self.movie_window = gtk.DrawingArea()
        self.window.add(self.movie_window)
        self.window.show_all()

        self.player = gst.element_factory_make("playbin", "player")
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.enable_sync_message_emission()
        bus.connect("message", self.on_message)
        bus.connect("sync-message::element", self.on_sync_message)

    def play(self, filepath):
        self.player.set_state(gst.STATE_NULL)
        self.player.set_property("uri", "file://" + filepath)
        self.player.set_state(gst.STATE_PLAYING)

    def on_message(self, bus, message):
        t = message.type
        if t == gst.MESSAGE_EOS:
            self.player.set_state(gst.STATE_NULL)
        elif t == gst.MESSAGE_ERROR:
            self.player.set_state(gst.STATE_NULL)
            err, debug = message.parse_error()
            print "Error: %s" % err, debug

    def on_sync_message(self, bus, message):
        if message.structure is None:
            return
        message_name = message.structure.get_name()
        if message_name == "prepare-xwindow-id":
            imagesink = message.src
            imagesink.set_property("force-aspect-ratio", True)
            imagesink.set_xwindow_id(self.movie_window.window.xid)

def main():
    parser = optparse.OptionParser(usage='%prog [options] url...', version="%prog")

    parser.add_option('-v', '--verbose', action="store_true",
                      dest='verbose', default=False,
                      help='print some debugging (default: %default)')
    parser.add_option('-p', '--path', action="store", metavar="PATH",
                      dest='path', default=None,
                      help='download files to PATH')
    parser.add_option('-n', '--number', action="store",
                      dest='n', default=1, type="int",
                      help='number of player to start (default: %default)')

    options, args = parser.parse_args()

    if len(args) == 0:
        parser.print_help()
        sys.exit(1)
    
    if options.verbose:
        logging.basicConfig(level=logging.DEBUG)

    for url in args:
        for l in range(options.n):
            p = GSTPlayer()
            fetcher = HLSFetcher(url, options.path, p)
            fetcher.start()

    import pdb
    pdb.set_trace()
    reactor.run()


if __name__ == '__main__':
    gtk.gdk.threads_init()
    sys.exit(main())
