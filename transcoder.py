#!/usr/bin/env python

import argparse
import logging
import os
import re
import subprocess

from itertools import islice

DEFAULT_LOGLEVEL = logging.WARNING
DEFAULT_FFMPEG_LOGLEVEL = 'error'
DEFAULT_LANGUAGES = ['eng', 'rus']
SURROUND_FORMATS = ['ac3', 'dts']
FORMATS = ['h264', 'aac', 'ac3', 'dts', 'subrip', 'mov_text']
FFPROBE_REGEX = re.compile('Stream #(?P<file_id>\d+):(?P<track_id>\d+)'
                           '\((?P<language>\w+)\):\s+(?P<track_type>\w+):\s+'
                           '(?P<format>\w+).*')


class Track(object):
    def __init__(self, file_id, track_id, language, format,
                 trackfile=None, temporary=False,
                 *args, **kwargs):
        self.file_id = file_id
        self.track_id = track_id
        self.language = language
        self.format = format
        self.trackfile = trackfile
        self.temporary = temporary

    def __repr__(self):
        return '%d:%d:%s:%s (%s)' % (self.file_id, self.track_id,
                                     self.language, self.format,
                                     self.trackfile)

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

    def key(self, languages):
        return (
            languages.index(self.language),
            FORMATS.index(self.format)
        )

    @property
    def map(self):
        return '%d:%d' % (self.file_id, self.track_id)


class Transcoder(object):
    def __init__(self, source, languages, ffmpeg_loglevel):
        self.source = source
        self.basename = os.path.splitext(os.path.basename(self.source))[0]
        self.languages = languages
        self.ffmpeg_loglevel = ffmpeg_loglevel
        self.video_tracks = []
        self.audio_tracks = []
        self.subs_tracks = []

    def __del__(self):
        tracks = self.video_tracks + self.audio_tracks + self.subs_tracks
        for t in filter(lambda x: x.temporary, tracks):
            os.unlink(t.trackfile)

    def probe(self):
        output = subprocess.check_output(['ffprobe', '-i', self.source],
                                         stderr=subprocess.STDOUT)
        for line in output.split('\n'):
            m = FFPROBE_REGEX.match(line.strip())
            if not m:
                continue

            d = m.groupdict()
            if d['language'] not in self.languages:
                continue

            if d['format'] not in FORMATS:
                continue

            t = Track(trackfile=self.source, **d)
            if d['track_type'] == 'Video':
                self.video_tracks.append(t)
            elif d['track_type'] == 'Audio':
                self.audio_tracks.append(t)
            elif d['track_type'] == 'Subtitle':
                self.subs_tracks.append(t)

    def convert_audio(self, track):
        wav = '%s.%s.wav' % (self.basename, track.language)
        m4a = '%s.%s.m4a' % (self.basename, track.language)

        cmd = [
            'ffmpeg',
            '-loglevel', self.ffmpeg_loglevel,
            '-i', self.source,
            '-map', track.map,
            '-f', 'wav',
            '-ac', '2',
            '-y', wav
        ]

        logging.info('Extracting audio track %s as WAV', track)
        logging.debug('FFMPEG command: %s', ' '.join(cmd))
        subprocess.call(cmd)

        cmd = [
            'afconvert',
            '-q', '127',
            '-s', '3',
            '-f', 'm4af',
            '-d', 'aac',
            '-u', 'vbrq', '127',
            wav, m4a
        ]

        logging.info('Converting audio track %s to AAC', track)
        logging.debug('AfConvert command: %s', ' '.join(cmd))
        subprocess.call(cmd)

        logging.debug('Deleting temporary WAV file: %s' % wav)
        os.unlink(wav)

        return Track(file_id=self.languages.index(track.language) + 1,
                     track_id=0,
                     language=track.language,
                     format='aac',
                     trackfile=m4a,
                     temporary=True)

    def transcode(self):
        for track in islice(self.audio_tracks, len(self.audio_tracks)):
            if track.format in SURROUND_FORMATS:
                converted_track = self.convert_audio(track)
                self.audio_tracks.append(converted_track)

        self.video_tracks = sorted(self.video_tracks,
                                   key=lambda x: x.key(self.languages))
        self.audio_tracks = sorted(self.audio_tracks,
                                   key=lambda x: x.key(self.languages))
        self.subs_tracks = sorted(self.subs_tracks,
                                  key=lambda x: x.key(self.languages))

        cmd = ['ffmpeg']
        cmd += ['-loglevel', self.ffmpeg_loglevel]
        cmd += ['-i', self.source]

        # Additional input files

        for v in filter(lambda x: x.temporary, self.video_tracks):
            cmd += ['-i', v.trackfile]

        for a in filter(lambda x: x.temporary, self.audio_tracks):
            cmd += ['-i', a.trackfile]

        for s in filter(lambda x: x.temporary, self.subs_tracks):
            cmd += ['-i', s.trackfile]

        # Set up track mapping

        for v in self.video_tracks:
            cmd += ['-map', v.map]

        for a in self.audio_tracks:
            cmd += ['-map', a.map]

        for s in self.subs_tracks:
            cmd += ['-map', s.map]

        # Set up track codecs

        for i in range(len(self.video_tracks)):
            cmd += ['-c:v:%d' % i, 'copy']

        for i in range(len(self.audio_tracks)):
            cmd += ['-c:a:%d' % i, 'copy']

        for i in range(len(self.subs_tracks)):
            cmd += ['-c:s:%d' % i, 'mov_text']

        # Track metadata

        for i in range(len(self.video_tracks)):
            v = self.video_tracks[i]
            cmd += ['-metadata:s:v:%d' % i, 'language=%s' % v.language]

        for i in range(len(self.audio_tracks)):
            a = self.audio_tracks[i]
            cmd += ['-metadata:s:a:%d' % i, 'language=%s' % a.language]

        for i in range(len(self.subs_tracks)):
            s = self.subs_tracks[i]
            cmd += ['-metadata:s:s:%d' % i, 'language=%s' % s.language]

        # Output options

        filename = self.basename + '.transcoded.m4v'

        cmd += ['-f', 'mp4']
        cmd += ['-y', filename]

        logging.debug('FFMPEG command: %s', ' '.join(cmd))

        # Print file layout

        logstring = 'Creating M4V file with the following layout:'
        logtemplate = '\n      %-11s%d:%s:%s'
        n = 0
        for v in self.video_tracks:
            logstring += logtemplate % ('Audio:', n, v.language, v.format)
            n += 1
        for a in self.audio_tracks:
            logstring += logtemplate % ('Video:', n, a.language, a.format)
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
            values = DEFAULT_LANGUAGES
        setattr(args, self.dest, values)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Transcode file to m4v.')
    parser.add_argument('source', metavar='SOURCE', nargs='+',
                        help='source file(s)')
    parser.add_argument('-l', dest='languages', action=LanguagesAction,
                        help='list of languages. eng,rus by default')

    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument('-v', action='store_const', dest='loglevel',
                                 const=logging.INFO)
    verbosity_group.add_argument('-vv', action='store_const', dest='loglevel',
                                 const=logging.DEBUG)

    parser.add_argument('-fv', action='store', dest='ffmpeg_loglevel',
                        metavar='LEVEL', help='ffmpeg log level')
    args = parser.parse_args()

    handler = logging.StreamHandler()
    handler.setFormatter(LogFormatter())
    logger = logging.getLogger()
    logger.setLevel(args.loglevel or DEFAULT_LOGLEVEL)
    logger.addHandler(handler)

    for source in args.source:
        t = Transcoder(
            source=source,
            languages=args.languages or DEFAULT_LANGUAGES,
            ffmpeg_loglevel=args.ffmpeg_loglevel or DEFAULT_FFMPEG_LOGLEVEL
        )
        t.probe()
        t.transcode()
