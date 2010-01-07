#!/usr/bin/env python

import sys
import urlparse
import optparse
import os, os.path
import logging
import tempfile
import codecs
from itertools import ifilter

import pygtk, gtk, gobject
gobject.threads_init()
from twisted.internet import gtk2reactor, defer
from twisted.internet.task import deferLater

gtk2reactor.install()
from twisted.internet import reactor
from twisted.web import client

if sys.version_info < (2, 4):
    raise ImportError("Cannot run with Python version < 2.4")

def to_dict(l):
    i = (f.split('=') for f in l.split(','))
    d = dict((k.strip(), v.strip()) for (k,v) in i)
    return d

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

    def get_program_playlist(self, program_id=None, bandwidth=None):
        # return the (uri, dict) of the best matching playlist
        if not self.has_programs():
            raise
        return (self._programs[0]['uri'], self._programs[0])

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
        return d

    def has_files(self):
        return len(self._files) != 0

    def iter_files(self):
        # return an iter on the playlist media files
        if not self.has_files():
            return

        if not self._endlist:
            current = max(self._first_sequence, self._last_sequence - 3)
        else:
            # tread differently on-demand playlists?
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
            logging.debug("Content didn't change")
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
            logging.debug('Invalid first line: %r' % first_line)
            raise

        self.target_duration = None
        discontinuity = False
        allow_cache = None
        i = 0
        new_files = []
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
            logging.debug("Invalid HLS stream: no programs & no duration")
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

class HLSFetcher(object):

    def __init__(self, url, path=None, program=1, bitrate=200000):
        self.url = url
        self.path = path
        if not self.path:
            self.path = tempfile.mkdtemp()
        self.program = program
        self.bitrate = bitrate

        self._program_playlist = None
        self._file_playlist = None
        self._cookies = {}
        self._cached_files = {}

        self._files = None # the iter of the playlist files download
        self._next_download = None # the delayed download defer, if any
        self._file_playlisted = None # the defer to wait until new files are added to playlist

    def _get_page(self, url):
        url = url.encode("utf-8")
        return client.getPage(url, cookies=self._cookies)

    def _download_page(self, url, path):
        # client.downloadPage does not support cookies!
        def _check(x):
            logging.debug(len(x))
            return x

        d = self._get_page(url)
        f = open(path, 'w')
        d.addCallback(_check)
        d.addCallback(lambda x: f.write(x))
        d.addBoth(lambda _: f.close())
        d.addCallback(lambda _: path)
        return d

    def delete(self, f):
        keys = self._cached_files.keys()
        for i in ifilter(f, keys):
            filename = self._cached_files[i]
            logging.debug("Removing %r" % filename)
            os.remove(filename)
            del self._cached_files[i]
        self._cached_files

    def _got_file(self, path, l, f):
        logging.debug("got " + l + " in " + path)
        self._cached_files[f['sequence']] = path
        if self._new_filed:
            self._new_filed.callback((path, l, f))
            self._new_filed = None
        return (path, l, f)

    def _download_file(self, f):
        l = make_url(self._file_playlist.url, f['file'])
        name = urlparse.urlparse(f['file']).path.split('/')[-1]
        path = os.path.join(self.path, name)
        d = self._download_page(l, path)
        d.addCallback(self._got_file, l, f)
        return d

    def _get_next_file(self, last_file=None):
        next = self._files.next()
        if next:
            delay = 0
            if last_file:
                if not self._cached_files.has_key(last_file['sequence'] - 1) or \
                        not self._cached_files.has_key(last_file['sequence'] - 2):
                    delay = 0
                elif self._file_playlist.endlist():
                    delay = 1
                else:
                    delay = last_file['duration']
            return deferLater(reactor, delay, self._download_file, next)
        elif not self._file_playlist.endlist():
            self._file_playlisted = defer.Deferred()
            self._file_playlisted.addCallback(lambda x: self._get_next_file(last_file))
            return self._file_playlisted

    def _handle_end(self, failure):
        failure.trap(StopIteration)
        print "End of media"
        reactor.stop()

    # FIXME should be properly scheduled differently
    def _get_files_loop(self, last_file=None):
        if last_file:
            (path, l, f) = last_file
        else:
            f = None
        d = self._get_next_file(f)
        # and loop
        d.addCallback(self._get_files_loop)
        d.addErrback(self._handle_end)

    def _playlist_updated(self, pl):
        if pl.has_programs():
            # if we got a program playlist, save it and start a program
            self._program_playlist = pl
            (program_url, _) = pl.get_program_playlist(self.program, self.bitrate)
            l = make_url(self.url, program_url)
            return self._reload_playlist(M3U8(l))
        elif pl.has_files():
            # we got sequence playlist, start reloading it regularly, and get files
            self._file_playlist = pl
            if not self._files:
                self._files = pl.iter_files()
            if not pl.endlist():
                reactor.callLater(pl.reload_delay(), self._reload_playlist, pl)
            if self._file_playlisted:
                self._file_playlisted.callback(pl)
                self._file_playlisted = None
        else:
            raise
        return pl

    def _got_playlist_content(self, content, pl):
        if not pl.update(content):
            # if the playlist cannot be loaded, start a reload timer
            return deferLater(reactor, pl.reload_delay(), self._reload_playlist, pl)
        return pl

    def _reload_playlist(self, pl):
        logging.debug('fetching %r' % pl.url)
        d = self._get_page(pl.url).addCallback(self._got_playlist_content, pl)
        d.addCallback(self._playlist_updated)
        return d

    def get_file(self, sequence):
        d = defer.Deferred()
        keys = self._cached_files.keys()
        try:
            sequence = ifilter(lambda x: x >= sequence, keys).next()
            filename = self._cached_files[sequence]
            d.callback(filename)
        except:
            d.addCallback(lambda x: self.get_file(sequence))
            self._new_filed = d
            keys.sort()
            logging.debug('waiting for %r (available: %r)' % (sequence, keys))
        return d

    def start(self):
        self._files = None
        d = self._reload_playlist(M3U8(self.url))
        d.addCallback(lambda _: self._get_files_loop())
        self._new_filed = defer.Deferred()
        return self._new_filed

    def stop(self):
        pass

class HLSControler:

    def __init__(self, fetcher=None):
        self.fetcher = fetcher
        self.player = None
        self._player_sequence = None

    def set_player(self, player):
        self.player = player
        if player:
            self.player.connect_about_to_finish(self.on_player_about_to_finish)

    def _start(self, first_file):
        (path, l, f) = first_file
        self._player_sequence = f['sequence']
        if self.player:
            self.player.set_uri(path)
            self.player.play()

    def start(self):
        d = self.fetcher.start()
        d.addCallback(self._start)

    def _set_next_uri(self):
        # keep only the past three segments
        self.fetcher.delete(lambda x: x <= self._player_sequence - 3)
        self._player_sequence += 1
        d = self.fetcher.get_file(self._player_sequence)
        d.addCallback(self.player.set_uri)

    def on_player_about_to_finish(self):
        reactor.callFromThread(self._set_next_uri)

class GSTPlayer:

    def __init__(self, with_playbin=False, gapless=False):
        import pygst
        import gst
        print "BLAH"
        self.gapless = False
        self.with_appsrc = not with_playbin

        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        self.window.set_title("Video-Player")
        self.window.set_default_size(500, 400)
        self.window.set_type_hint(gtk.gdk.WINDOW_TYPE_HINT_DIALOG)
        self.window.connect('delete-event', lambda _: reactor.stop())
        self.movie_window = gtk.DrawingArea()
        self.window.add(self.movie_window)
        self.window.show_all()

        if self.with_appsrc:
            self.player = gst.Pipeline("player")
            self.appsrc = gst.element_factory_make("appsrc", "source")
            self.appsrc.connect("enough-data", self.on_enough_data)
            self.appsrc.connect("need-data", self.on_need_data)
            self.appsrc.set_property("max-bytes", 10000)
            if 'HLS_PLAYER_DEBUG' in os.environ.keys():
                decodebin = gst.element_factory_make("identity", "decodebin")
            else:
                decodebin = gst.element_factory_make("decodebin2", "decodebin")
                decodebin.connect("new-decoded-pad", self.on_decoded_pad)
            self.player.add(self.appsrc, decodebin)
            gst.element_link_many(self.appsrc, decodebin)
            if 'HLS_PLAYER_DEBUG' in os.environ.keys():
                def on_new_pad(demux, pad):
                    sink = gst.element_factory_make("fakesink", "fakesink")
                    self.player.add(sink)
                    gst.element_link_many(demux, sink)
                demux = gst.element_factory_make("mpegtsdemux", "demux")
                self.player.add(demux)
                decodebin.connect("pad-added", on_new_pad)
                gst.element_link_many(decodebin, demux)
        else:
            self.player = gst.element_factory_make("playbin2", "player")

        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.enable_sync_message_emission()
        bus.connect("message", self.on_message)
        bus.connect("sync-message::element", self.on_sync_message)
        self._playing = False
        self._need_data = False
        self._cb = None

    def need_data(self):
        return self._need_data

    def play(self):
        import gst
        self.player.set_state(gst.STATE_PLAYING)
        self._playing = True

    def stop(self):
        import gst
        self.player.set_state(gst.STATE_NULL)
        self._playing = False

    def set_uri(self, filepath):
        import gst
        logging.debug("set uri %r" % filepath)
        if self.with_appsrc:
            f = open(filepath)
            self.appsrc.emit('push-buffer', gst.Buffer(f.read()))
        elif self.gapless:
            self.player.set_property("uri", "file://" + filepath)
        else:
            playing = self._playing
            self.stop()
            self.player.set_property("uri", "file://" + filepath)
            if playing:
                self.play()

    def set_gapless(self, is_gapless):
        self.gapless = is_gapless

    def on_message(self, bus, message):
        import gst
        t = message.type
        if t == gst.MESSAGE_EOS:
            self.player.set_state(gst.STATE_NULL)
        elif t == gst.MESSAGE_ERROR:
            self.player.set_state(gst.STATE_NULL)
            err, debug = message.parse_error()
            print "Error: %s" % err, debug
        elif t == gst.MESSAGE_STATE_CHANGED:
            if message.src == self.player:
                o, n, p = message.parse_state_changed()
                print "State changed from %r to %r pending %r" % (o, n, p)

    def on_sync_message(self, bus, message):
        print "Message: %r" % (message,)
        if message.structure is None:
            return
        message_name = message.structure.get_name()
        if message_name == "prepare-xwindow-id":
            imagesink = message.src
            gtk.gdk.threads_enter()
            gtk.gdk.display_get_default().sync()
            imagesink.set_property("force-aspect-ratio", True)
            imagesink.set_xwindow_id(self.movie_window.window.xid)
            gtk.gdk.threads_leave()

    def on_decoded_pad(self, decodebin, pad, more_pad):
        import gst
        c = pad.get_caps().to_string()
        if "video" in c:
            q1 = gst.element_factory_make("queue", "vqueue")
            q1.props.max_size_buffers = 0
            q1.props.max_size_time = 0
            #q1.props.max_size_bytes = 0
            colorspace = gst.element_factory_make("ffmpegcolorspace", "colorspace")
            videosink = gst.element_factory_make("xvimagesink", "videosink")
            self.player.add(q1, colorspace, videosink)
            gst.element_link_many(q1, colorspace, videosink)
            for e in [q1, colorspace, videosink]:
                e.set_state(gst.STATE_PLAYING)
            sink_pad = q1.get_pad("sink")
            pad.link(sink_pad)
        elif "audio" in c:
            q2 = gst.element_factory_make("queue", "aqueue")
            q2.props.max_size_buffers = 0
            q2.props.max_size_time = 0
            #q2.props.max_size_bytes = 0
            audioconv = gst.element_factory_make("audioconvert", "audioconv")
            audioresample =  gst.element_factory_make("audioresample", "ar")
            audiosink = gst.element_factory_make("autoaudiosink", "audiosink")
            self.player.add(q2, audioconv, audioresample, audiosink)
            gst.element_link_many(q2, audioconv, audioresample, audiosink)
            for e in [q2, audioconv, audioresample, audiosink]:
                e.set_state(gst.STATE_PLAYING)
            sink_pad = q2.get_pad("sink")
            pad.link(sink_pad)

    def on_overrun(self, element):
        print "overrun on %r" % (element,)

    def on_underrun(self, element):
        print "underrun on %r" % (element,)

    def on_enough_data(self):
        logging.debug("Player is full up!");
        self._need_data = False;

    def on_need_data(self, src, length):
        logging.debug("Player is hungry! %r" % length);
        self._need_data = True;
        self._on_about_to_finish()

    def _on_about_to_finish(self, p=None):
        if self._cb:
            self._cb()

    def connect_about_to_finish(self, cb):
        if not self.with_appsrc:
            self.player.connect("about-to-finish", self._on_about_to_finish)
        self._cb = cb

def main():
    parser = optparse.OptionParser(usage='%prog [options] url...', version="%prog")

    parser.add_option('-v', '--verbose', action="store_true",
                      dest='verbose', default=False,
                      help='print some debugging (default: %default)')
    parser.add_option('-g', '--gapless', action="store_true",
                      dest='gapless', default=False,
                      help='play with gapless - very buggy (default: %default)')
    parser.add_option('-D', '--no-display', action="store_true",
                      dest='nodisplay', default=False,
                      help='display no video (default: %default)')
    parser.add_option('-p', '--path', action="store", metavar="PATH",
                      dest='path', default=None,
                      help='download files to PATH')
    parser.add_option('-P', '--playbin', action="store_true",
                      dest='playbin', default=False,
                      help='use playbin')
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
            if urlparse.urlsplit(url).scheme == '':
                url = "http://" + url
            p = None
            if not options.nodisplay:
                p = GSTPlayer(options.playbin)
                p.set_gapless(options.gapless)
            c = HLSControler(HLSFetcher(url, options.path))
            c.set_player(p)
            c.start()

    reactor.run()

if __name__ == '__main__':
    gtk.gdk.threads_init()
    sys.exit(main())
