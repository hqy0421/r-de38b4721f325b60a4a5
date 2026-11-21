#!/usr/bin/python3
# -*- coding: utf-8 -*-

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import export_json


class Test_export_json(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.outputfile = os.path.join(self.tempdir, 'export.jsonl')

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    #
    # helpers
    #

    def exporter(self, **config):
        return export_json.export_json(config=config or None)

    def export(self, sequence, data, exporter=None, **extra):
        if exporter is None:
            exporter = self.exporter()

        parameters = {'outputfile': self.outputfile, 'jsonl': True,
                      'sequence': sequence}
        parameters.update(extra)

        return exporter.process(parameters=parameters, data=data)

    def read_bytes(self):
        with open(self.outputfile, 'rb') as f:
            return f.read()

    def read_lines(self):
        with open(self.outputfile, 'r', encoding='utf-8') as f:
            return [json.loads(line) for line in f]

    def write_bytes(self, content):
        with open(self.outputfile, 'wb') as f:
            f.write(content)

    #
    # default (single document) behaviour
    #

    def test_default_mode_overwrites_file(self):
        exporter = self.exporter()

        exporter.process(parameters={'outputfile': self.outputfile},
                         data={'id': 'first'})
        self.assertEqual(json.loads(self.read_bytes().decode('utf-8')),
                         {'id': 'first'})

        exporter.process(parameters={'outputfile': self.outputfile},
                         data={'id': 'second', 'content_txt': 'ümläut'})

        content = self.read_bytes().decode('utf-8')
        self.assertEqual(json.loads(content),
                         {'id': 'second', 'content_txt': 'ümläut'})
        self.assertEqual(content.count('\n'), 0)

    def test_default_mode_not_jsonl(self):
        self.export(1, {'id': 'a'}, jsonl=False)

        # jsonl=False must fall back to the single document export
        self.assertEqual(json.loads(self.read_bytes().decode('utf-8')),
                         {'id': 'a'})

    def test_parameters_disable_config_jsonl(self):
        exporter = self.exporter(jsonl=True)

        exporter.process(parameters={'jsonl': False, 'sequence': 1,
                                     'outputfile': self.outputfile},
                         data={'id': 'a'})

        # single document, not JSONL
        self.assertEqual(json.loads(self.read_bytes().decode('utf-8')),
                         {'id': 'a'})

    #
    # JSONL basics
    #

    def test_append_sequence_and_record(self):
        self.export(1, {'id': 'a', 'title': 'Täst ünïcode'})
        self.export(2, {'id': 'b'})

        lines = self.read_lines()

        self.assertEqual(lines, [
            {'sequence': 1, 'record': {'id': 'a', 'title': 'Täst ünïcode'}},
            {'sequence': 2, 'record': {'id': 'b'}},
        ])

        # UTF-8, one line per record, terminated by newline
        raw = self.read_bytes()
        self.assertTrue(raw.endswith(b'\n'))
        self.assertIn('Täst ünïcode'.encode('utf-8'), raw)
        self.assertEqual(raw.count(b'\n'), 2)

    def test_embedded_newlines_stay_in_one_line(self):
        self.export(1, {'id': 'a', 'content_txt': 'line1\nline2\r\nline3'})

        raw = self.read_bytes()
        self.assertEqual(raw.count(b'\n'), 1)

        self.assertEqual(
            self.read_lines(),
            [{'sequence': 1,
              'record': {'id': 'a',
                         'content_txt': 'line1\nline2\r\nline3'}}])

    def test_mode_via_outputformat(self):
        parameters = {'outputfile': self.outputfile,
                      'outputformat': 'jsonl', 'sequence': 1}
        self.exporter().process(parameters=parameters, data={'id': 'a'})

        self.assertEqual(self.read_lines(),
                         [{'sequence': 1, 'record': {'id': 'a'}}])

    def test_sequence_number_string_accepted(self):
        self.export('1', {'id': 'a'})
        self.assertEqual(self.read_lines()[0]['sequence'], 1)

    #
    # sequence number validation
    #

    def test_missing_sequence_rejected(self):
        parameters = {'outputfile': self.outputfile, 'jsonl': True}

        with self.assertRaises(export_json.JsonlError):
            self.exporter().process(parameters=parameters, data={'id': 'a'})

        self.assertFalse(os.path.exists(self.outputfile))

    def test_zero_and_negative_sequence_rejected(self):
        for sequence in (0, -1):
            with self.assertRaises(export_json.JsonlSequenceError):
                self.export(sequence, {'id': 'a'})

        self.assertFalse(os.path.exists(self.outputfile))

    def test_first_sequence_must_be_one(self):
        with self.assertRaises(export_json.JsonlSequenceError):
            self.export(2, {'id': 'b'})

        self.assertFalse(os.path.exists(self.outputfile))

    def test_gap_rejected_and_file_unchanged(self):
        self.export(1, {'id': 'a'})
        before = self.read_bytes()

        with self.assertRaises(export_json.JsonlSequenceError):
            self.export(3, {'id': 'c'})

        self.assertEqual(self.read_bytes(), before)

    #
    # replay / conflict / stale sequence
    #

    def test_replay_identical_does_not_write(self):
        self.export(1, {'id': 'a'})
        self.export(2, {'id': 'b'})

        before = self.read_bytes()
        mtime_before = os.stat(self.outputfile).st_mtime_ns

        self.export(2, {'id': 'b'})

        self.assertEqual(self.read_bytes(), before)
        self.assertEqual(os.stat(self.outputfile).st_mtime_ns, mtime_before)
        self.assertEqual(len(self.read_lines()), 2)

    def test_replay_identical_normalises_representation(self):
        self.export(1, {'id': 'a', 'tags': ['x', 'y']})
        before = self.read_bytes()

        # same record, different in-memory representation
        self.export(1, {'tags': ('x', 'y'), 'id': 'a'})

        self.assertEqual(self.read_bytes(), before)

    def test_replay_different_is_conflict(self):
        self.export(1, {'id': 'a'})
        self.export(2, {'id': 'b'})

        before = self.read_bytes()

        with self.assertRaises(export_json.JsonlConflictError):
            self.export(2, {'id': 'CHANGED'})

        self.assertEqual(self.read_bytes(), before)
        self.assertEqual(len(self.read_lines()), 2)

    def test_older_sequence_is_skipped(self):
        self.export(1, {'id': 'a'})
        self.export(2, {'id': 'b'})
        self.export(3, {'id': 'c'})

        before = self.read_bytes()

        self.export(1, {'id': 'a'})
        self.export(2, {'id': 'b'})

        self.assertEqual(self.read_bytes(), before)
        self.assertEqual([line['sequence'] for line in self.read_lines()],
                         [1, 2, 3])

    def test_older_sequence_with_different_content_is_skipped(self):
        self.export(1, {'id': 'a'})
        self.export(2, {'id': 'b'})

        before = self.read_bytes()

        # Only the last record is compared; older ones are simply skipped.
        self.export(1, {'id': 'different'})

        self.assertEqual(self.read_bytes(), before)

    #
    # incomplete tail fragment
    #

    def test_incomplete_tail_is_truncated(self):
        self.export(1, {'id': 'a'})

        with open(self.outputfile, 'ab') as f:
            f.write(b'{"sequence":2,"record":{"id":"b"')

        self.export(2, {'id': 'b'})

        lines = self.read_lines()
        self.assertEqual([line['sequence'] for line in lines], [1, 2])
        self.assertEqual(lines[1]['record'], {'id': 'b'})
        self.assertTrue(self.read_bytes().endswith(b'}\n'))

    def test_garbage_tail_is_truncated(self):
        self.export(1, {'id': 'a'})

        with open(self.outputfile, 'ab') as f:
            f.write(b'\x00\xff garbage without newline')

        self.export(2, {'id': 'b'})

        self.assertEqual([line['sequence'] for line in self.read_lines()],
                         [1, 2])

    def test_fragment_only_file_is_truncated_before_first_record(self):
        self.write_bytes(b'{"sequence":1,"record":{"id":"a"')

        self.export(1, {'id': 'a'})

        self.assertEqual(self.read_lines(),
                         [{'sequence': 1, 'record': {'id': 'a'}}])

    def test_tail_fragment_after_last_record_is_dropped_on_replay(self):
        self.export(1, {'id': 'a'})

        with open(self.outputfile, 'ab') as f:
            f.write(b'{"sequence":2,"record":{"id":"b"')

        # the fragment is not a confirmed record, so replaying 1 is a no-op
        # but still cleans up the unfinished fragment
        self.export(1, {'id': 'a'})

        self.assertEqual(self.read_lines(),
                         [{'sequence': 1, 'record': {'id': 'a'}}])

    def test_last_sequence_after_success(self):
        exporter = self.exporter()

        self.export(1, {'id': 'a'}, exporter=exporter)
        self.assertEqual(exporter.last_sequence, 1)

        # a replay confirms the persisted sequence, it is not advanced
        self.export(1, {'id': 'a'}, exporter=exporter)
        self.assertEqual(exporter.last_sequence, 1)

        self.export(2, {'id': 'b'}, exporter=exporter)
        self.assertEqual(exporter.last_sequence, 2)

    def test_last_confirmed_sequence(self):
        exporter = self.exporter()

        self.assertEqual(exporter.last_confirmed_sequence(self.outputfile), 0)

        self.export(1, {'id': 'a'}, exporter=exporter)
        self.export(2, {'id': 'b'}, exporter=exporter)
        self.assertEqual(exporter.last_confirmed_sequence(self.outputfile), 2)

        # an unfinished fragment does not change the confirmed position
        with open(self.outputfile, 'ab') as f:
            f.write(b'{"sequence":3,"record":{"id":"c"')
        self.assertEqual(exporter.last_confirmed_sequence(self.outputfile), 2)

    def test_unserializable_record_keeps_file_intact(self):
        self.export(1, {'id': 'a'})

        with open(self.outputfile, 'ab') as f:
            f.write(b'{"sequence":2,"record":{')

        before = self.read_bytes()

        with self.assertRaises(TypeError):
            self.export(2, {'id': 'b', 'invalid': object()})

        # the pending fragment is only dropped once the new record exists
        self.assertEqual(self.read_bytes(), before)

    #
    # invalid last complete record
    #

    def test_invalid_last_record_raises_and_keeps_file(self):
        self.export(1, {'id': 'a'})

        with open(self.outputfile, 'ab') as f:
            f.write(b'{"not json"}\n')

        before = self.read_bytes()

        with self.assertRaises(export_json.JsonlCorruptError):
            self.export(2, {'id': 'b'})

        self.assertEqual(self.read_bytes(), before)

    def test_last_record_without_sequence_raises(self):
        self.write_bytes(b'{"record":{"id":"a"}}\n')
        before = self.read_bytes()

        with self.assertRaises(export_json.JsonlCorruptError):
            self.export(1, {'id': 'a'})

        self.assertEqual(self.read_bytes(), before)

    def test_last_record_invalid_utf8_raises(self):
        self.write_bytes(b'{"sequence":1,"record":"\xff"}\n')
        before = self.read_bytes()

        with self.assertRaises(export_json.JsonlCorruptError):
            self.export(2, {'id': 'b'})

        self.assertEqual(self.read_bytes(), before)

    #
    # durability
    #

    def test_new_line_is_synced_before_returning(self):
        exporter = self.exporter()

        real_fsync = os.fsync
        synced = []

        def tracking_fsync(fd):
            if os.path.isdir('/proc/self/fd/{}'.format(fd)):
                # directory fsync (install of the new file)
                return real_fsync(fd)

            synced.append(os.fstat(fd).st_size)
            return real_fsync(fd)

        with mock.patch.object(export_json.os, 'fsync',
                               side_effect=tracking_fsync):
            self.export(1, {'id': 'a'}, exporter=exporter)

        # the file size at the time of the fsync includes the new line
        self.assertEqual(synced[-1], os.stat(self.outputfile).st_size)
        self.assertGreater(synced[-1], 0)

    def test_no_write_on_rejected_record(self):
        self.export(1, {'id': 'a'})

        with mock.patch.object(export_json.os, 'fsync') as fsync:
            with self.assertRaises(export_json.JsonlSequenceError):
                self.export(5, {'id': 'e'})

            fsync.assert_not_called()

    #
    # tail only recovery
    #

    def test_recovery_reads_only_tail(self):
        for sequence in range(1, 200):
            self.export(sequence, {'id': 'record-{}'.format(sequence),
                                   'content': 'x' * 100})

        size = os.stat(self.outputfile).st_size
        self.assertGreater(size, 10000)

        exporter = self.exporter(jsonl_tail_chunk_size=512)

        class CountingFile(object):

            def __init__(self, file):
                self.file = file
                self.bytes_read = 0

            def seek(self, *args):
                return self.file.seek(*args)

            def read(self, size=-1):
                data = self.file.read(size)
                self.bytes_read += len(data)
                return data

        with open(self.outputfile, 'rb') as real_file:
            counting = CountingFile(real_file)
            previous, last = exporter._find_last_record_bounds(counting, size)

        self.assertIsNotNone(previous)
        self.assertIsNotNone(last)

        # only the tail was inspected, not the whole history
        self.assertLess(counting.bytes_read, size // 2)

    def test_last_record_bigger_than_chunk(self):
        self.export(1, {'id': 'small'})
        self.export(2, {'id': 'big', 'content': 'x' * 5000})

        exporter = self.exporter(jsonl_tail_chunk_size=256)

        with open(self.outputfile, 'rb') as real_file:
            size = os.fstat(real_file.fileno()).st_size
            previous, last = exporter._find_last_record_bounds(real_file, size)

            self.assertIsNotNone(previous)
            self.assertIsNotNone(last)

            sequence, record = exporter._read_record(
                real_file, self.outputfile, previous + 1, last)

        self.assertEqual(sequence, 2)
        self.assertEqual(record['content'], 'x' * 5000)

    #
    # configuration
    #

    def test_outputfile_from_config(self):
        exporter = self.exporter(outputfile=self.outputfile)

        exporter.process(parameters={'jsonl': True, 'sequence': 1},
                         data={'id': 'a'})

        self.assertEqual(self.read_lines(),
                         [{'sequence': 1, 'record': {'id': 'a'}}])

    def test_jsonl_and_sequence_from_config(self):
        exporter = self.exporter(jsonl=True, sequence=1,
                                 outputfile=self.outputfile)

        exporter.process(parameters={}, data={'id': 'a'})

        self.assertEqual(self.read_lines(),
                         [{'sequence': 1, 'record': {'id': 'a'}}])

    def test_commit(self):
        self.exporter().commit()


if __name__ == '__main__':
    unittest.main()
