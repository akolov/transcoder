#!/usr/bin/env python

import argparse
import logging
import os
import re
import subprocess
import tempfile

from collections import OrderedDict
from itertools import islice
from shutil import rmtree

DEFAULT_LOGLEVEL = logging.WARNING
DEFAULT_FFMPEG_LOGLEVEL = 'error'
DEFAULT_LANGUAGES = OrderedDict([
    ('eng', 'English'),
    ('rus', 'Russian'),
    ('jpn', 'Japanese')
])

FORMATS_VIDEO = ['h264']
FORMATS_STEREO = ['aac']
FORMATS_LOSSLESS = ['flac']
FORMATS_SURROUND = ['ac3', 'dts']
FORMATS_CONVERT = ['vorbis']
FORMATS_SUBTITLE = ['subrip', 'mov_text']
FORMATS = FORMATS_VIDEO + FORMATS_STEREO + FORMATS_CONVERT + FORMATS_SURROUND + FORMATS_LOSSLESS + FORMATS_SUBTITLE

FFPROBE_REGEX = re.compile('Stream #(?P<file_id>\d+):(?P<track_id>\d+)'
                           '(?:\((?P<language>\w+)\))?:\s+(?P<track_type>\w+):\s+'
                           '(?P<format>\w+)(\s*(\((?P<subformat>.*)\))?,\s+(?P<other>.*))?')

FFMPEG_PATH = 'ffmpeg'


class Track(object):
    def __init__(self, file_id, track_id, language, format,
                 trackfile=None, temporary=False, codec='copy',
                 surround=False, options=None, *args, **kwargs):
        self.file_id = file_id
        self.track_id = track_id
        self.language = language
        self.format = format
        self.trackfile = trackfile
        self.temporary = temporary
        self.codec = codec
        self.surround = surround
        self.options = options

    def __repr__(self):
        return '%d:%d:%s:%s (%s) -> %s' % (self.file_id, self.track_id,
                                           self.language, self.format,
                                           self.trackfile, self.codec)

    @property
    def file_id(self):
        return self._file_id

    @file_id.setter
    def file_id(self, value):
        self._file_id = int(value)

    @property
    def track_id(self):
        return self._track_id

    @track_id.setter
    def track_id(self, value):
        self._track_id = int(value)

    def name(self, include_language):
        _name = None

        if self.format in FORMATS_VIDEO:
            _name = 'Video'
        elif self.format in FORMATS_STEREO:
            _name = 'Stereo'
        elif self.format in FORMATS_SURROUND:
            _name = 'Surround'
        elif self.format in FORMATS_LOSSLESS:
            _name = 'Lossless'
        elif self.format in FORMATS_SUBTITLE:
            _name = 'Subtitles'

        if include_language and _name:
            if self.language in DEFAULT_LANGUAGES:
                _name = '%s %s' % (DEFAULT_LANGUAGES[self.language], _name)
            else:
                _name = '%s %s' % (self.language, _name)

        return _name

    def key(self, languages):
        return (
            languages.index(self.language),
            FORMATS.index(self.format)
        )

    @property
    def map(self):
        return '%d:%d' % (self.file_id, self.track_id)


class Transcoder(object):
    def __init__(self, source, languages, force_language, ffmpeg_loglevel):
        self.source = source
        self.basename = os.path.splitext(os.path.basename(self.source))[0]
        self.languages = languages
        self.force_language = force_language
        self.ffmpeg_loglevel = ffmpeg_loglevel
        self.video_tracks = []
        self.audio_tracks = []
        self.subs_tracks = []
        self.temp_dir = tempfile.mkdtemp(prefix='transcoder-')

        if not self.temp_dir:
            raise IOError()

    def __del__(self):
        rmtree(self.temp_dir)

    def probe(self):
        output = subprocess.check_output(['ffprobe', '-i', self.source],
                                         stderr=subprocess.STDOUT)
        for line in output.split('\n'):
            m = FFPROBE_REGEX.match(line.strip())
            if not m:
                continue

            d = m.groupdict()
            if self.force_language and not d['language']:
                logging.info('Forcing %s track language to %s',
                             d['track_type'], self.force_language)
                d['language'] = self.force_language
            elif d['language'] not in self.languages:
                logging.info('Skipping track language: %s', d['language'])
                continue

            if d['format'] not in FORMATS:
                logging.info('Unknown track format: %s', d['format'])
                continue

            t = Track(trackfile=self.source, **d)
            logging.debug('Found track %s', t)

            if d['track_type'] == 'Video':
                self.video_tracks.append(t)
            elif d['track_type'] == 'Audio':
                if ('5.1(side)' in d['other']):
                    t.surround = True
                self.audio_tracks.append(t)
            elif d['track_type'] == 'Subtitle':
                self.subs_tracks.append(t)

    def dts_convert_audio(self, track):
        return Track(file_id=track.file_id,
                     track_id=track.track_id,
                     language=track.language,
                     format='dts',
                     temporary=False,
                     codec='dca')

    def aac_convert_audio(self, track):
        return Track(file_id=track.file_id,
                     track_id=track.track_id,
                     language=track.language,
                     format='aac',
                     temporary=False,
                     codec='libfdk_aac',
                     options=['-ac', '2', '-b:a', '192k', '-cutoff', '18000'])

    def transcode(self):
        if (not self.video_tracks and
                not self.audio_tracks and
                not self.subs_tracks):
            logging.error('Nothing to convert. Exiting.')
            return

        for track in islice(self.audio_tracks, len(self.audio_tracks)):
            if track.format in FORMATS_SURROUND:
                converted_track = self.aac_convert_audio(track)
                self.audio_tracks.append(converted_track)
            elif track.format in FORMATS_CONVERT:
                converted_track = self.aac_convert_audio(track)
                self.audio_tracks.append(converted_track)
                self.audio_tracks.remove(track)
            elif track.format in FORMATS_LOSSLESS:
                if track.surround:
                    dts_converted_track = self.dts_convert_audio(track)
                    self.audio_tracks.append(dts_converted_track)
                aac_converted_track = self.aac_convert_audio(track)
                self.audio_tracks.append(aac_converted_track)
                self.audio_tracks.remove(track)

        self.video_tracks = sorted(self.video_tracks, key=lambda x: x.key(self.languages))
        self.audio_tracks = sorted(self.audio_tracks, key=lambda x: x.key(self.languages))
        self.subs_tracks = sorted(self.subs_tracks, key=lambda x: x.key(self.languages))

        cmd = [FFMPEG_PATH]
        cmd += ['-loglevel', self.ffmpeg_loglevel]
        cmd += ['-i', self.source]

        # Additional input files

        num_files = 1

        for v in filter(lambda x: x.temporary, self.video_tracks):
            v.file_id = num_files
            cmd += ['-i', v.trackfile]
            num_files += 1

        for a in filter(lambda x: x.temporary, self.audio_tracks):
            a.file_id = num_files
            cmd += ['-i', a.trackfile]
            num_files += 1

        for s in filter(lambda x: x.temporary, self.subs_tracks):
            a.file_id = num_files
            cmd += ['-i', s.trackfile]
            num_files += 1

        # Set up track mapping

        for v in self.video_tracks:
            cmd += ['-map', v.map]

        for a in self.audio_tracks:
            cmd += ['-map', a.map]

        for s in self.subs_tracks:
            cmd += ['-map', s.map]

        # Set up track codecs

        for i in range(len(self.video_tracks)):
            track = self.video_tracks[i]
            cmd += ['-c:v:%d' % i, track.codec]
            if track.options:
                cmd += track.options

        for i in range(len(self.audio_tracks)):
            track = self.audio_tracks[i]
            cmd += ['-c:a:%d' % i, track.codec]
            if track.options:
                cmd += track.options

        for i in range(len(self.subs_tracks)):
            cmd += ['-c:s:%d' % i, 'mov_text']
            if track.options:
                cmd += track.options

        # Track metadata

        video_languages = set([v.language for v in self.video_tracks])
        audio_languages = set([a.language for a in self.audio_tracks])

        show_video_language = len(video_languages) > 1
        show_audio_language = len(audio_languages) > 1

        for i in range(len(self.video_tracks)):
            v = self.video_tracks[i]
            cmd += ['-metadata:s:v:%d' % i, 'language=%s' % v.language]
            cmd += ['-metadata:s:v:%d' % i, 'title=%s' % v.name(show_video_language)]

        for i in range(len(self.audio_tracks)):
            a = self.audio_tracks[i]
            cmd += ['-metadata:s:a:%d' % i, 'language=%s' % a.language]
            cmd += ['-metadata:s:a:%d' % i, 'title=%s' % a.name(show_audio_language)]

        for i in range(len(self.subs_tracks)):
            s = self.subs_tracks[i]
            cmd += ['-metadata:s:s:%d' % i, 'language=%s' % s.language]
            cmd += ['-metadata:s:s:%d' % i, 'title=%s' % s.name(True)]

        # Output options

        for a in self.audio_tracks:
            if a.codec == 'dca':
                cmd += ['-strict', '-2']
                break

        filename = self.basename + '.transcoded.m4v'

        cmd += ['-f', 'mp4']
        cmd += ['-y', filename]

        logging.debug('FFMPEG command: %s', ' '.join(cmd))

        # Print file layout

        logstring = 'Creating M4V file with the following layout:'
        logtemplate = '\n      %-11s%d:%s:%s'
        n = 0
        for v in self.video_tracks:
            logstring += logtemplate % ('Video:', n, v.language, v.format)
            n += 1
        for a in self.audio_tracks:
            logstring += logtemplate % ('Audio:', n, a.language, a.format)
            n += 1
        for s in self.subs_tracks:
            logstring += logtemplate % ('Subtitle:', n, s.language, s.format)
            n += 1
        logging.info(logstring)

        logging.info('FFMPEG is running now...')
        subprocess.check_call(cmd)

        logging.info('Done. Your new file is: %s' % filename)


class LogFormatter(logging.Formatter):
    def format(self, record):
        record.msg = '[%s] %s' % (record.levelname[0], record.msg)
        return super(LogFormatter, self).format(record)


class LanguagesAction(argparse.Action):
    def __call__(self, parser, args, values, option_string=None):
        if not values:
            values = 'eng,rus'
        try:
            values = [v.strip() for v in values.strip().split(',')]
        except ValueError:
            values = DEFAULT_LANGUAGES.keys()
        setattr(args, self.dest, values)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Transcode file to m4v.')
    parser.add_argument('source', metavar='SOURCE', nargs='+',
                        help='source file(s)')
    parser.add_argument('-l', dest='languages', action=LanguagesAction,
                        help='list of languages. eng,rus by default')
    parser.add_argument('-f', dest='force_language',
                        help='force unknown language')

    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument('-v', action='store_const', dest='loglevel',
                                 const=logging.INFO)
    verbosity_group.add_argument('-vv', action='store_const', dest='loglevel',
                                 const=logging.DEBUG)

    parser.add_argument('-p', action='store', dest='ffmpeg_path',
                        metavar='FFMPEG_PATH', help='path to ffmpeg binary')
    parser.add_argument('-d', action='store', dest='ffmpeg_loglevel',
                        metavar='LEVEL', help='ffmpeg log level')

    args = parser.parse_args()

    handler = logging.StreamHandler()
    handler.setFormatter(LogFormatter())
    logger = logging.getLogger()
    logger.setLevel(args.loglevel or DEFAULT_LOGLEVEL)
    logger.addHandler(handler)

    if args.ffmpeg_path:
        FFMPEG_PATH = args.ffmpeg_path

    for source in args.source:
        t = Transcoder(
            source=source,
            languages=args.languages or DEFAULT_LANGUAGES.keys(),
            force_language=args.force_language,
            ffmpeg_loglevel=args.ffmpeg_loglevel or DEFAULT_FFMPEG_LOGLEVEL
        )
        t.probe()
        t.transcode()
