#!/usr/bin/python3
# -*- coding: utf-8 -*-

import json
import os
import sys

#
# JSON data exporter
#
# Default mode writes one single JSON document, overwriting the output file
# on every call (this behaviour is unchanged).
#
# Optional JSONL continuation mode
# --------------------------------
#
# Batch exports of an upstream that delivers records one by one with a
# consecutive sequence number starting at 1 (and retries from the last
# confirmed position after a failure) can use a UTF-8 JSON Lines file:
#
#     parameters['jsonl'] = True
#     parameters['sequence'] = <source sequence number, starting at 1>
#     parameters['outputfile'] = <path>
#
# Every line contains the source sequence number and the record:
#
#     {"sequence":1,"record":{...}}\n
#
# The newline is the record terminator. On resume/retry:
#
#   * only an unfinished trailing fragment (bytes after the last newline) is
#     truncated,
#   * the last newline-terminated record determines the confirmed position;
#     if that record is invalid, a JsonlCorruptError is raised and the file is
#     left untouched,
#   * replaying the last sequence with identical content does not write again,
#   * replaying the last sequence with different content is a conflict,
#   * sequences older than the last one are skipped,
#   * any other sequence (a gap) is rejected,
#   * the new line is only confirmed after it has been written and fsynced.
#
# After a successful process() the confirmed source sequence number is
# available as exporter.last_sequence. last_confirmed_sequence(path) returns
# the position to resume from.
#
# Recovery only reads the end of the file, in bounded chunks (see the
# config option jsonl_tail_chunk_size), instead of reading the whole history.
#
# Only one writer per file is supported.
#

DEFAULT_TAIL_CHUNK_SIZE = 64 * 1024

SEQUENCE_KEY = 'sequence'
RECORD_KEY = 'record'

# Accepted aliases when reading a persisted line, so a file produced by an
# interchangeable writer can still be resumed. New lines are always written
# with SEQUENCE_KEY and RECORD_KEY.
SEQUENCE_KEY_ALIASES = (SEQUENCE_KEY,
                        'sequence_number', 'sequenceNumber',
                        'seqno', 'seq_no', 'seq',
                        'source_sequence', 'source_seq', 'number')
RECORD_KEY_ALIASES = (RECORD_KEY, 'data', 'document', 'doc', 'payload')

JSONL_FORMATS = ('jsonl', 'json-lines', 'jsonlines', 'ndjson', 'json_lines')

JSONL_ENABLE_OPTIONS = ('jsonl', 'json_lines', 'jsonlines')
JSONL_FORMAT_OPTIONS = ('outputformat', 'output_format', 'format')
SEQUENCE_OPTIONS = ('sequence', 'sequence_number', 'sequence_no',
                    'seqno', 'seq_no', 'source_sequence', 'record_number')


class JsonlError(ValueError):
    """Base class for errors raised by the JSONL continuation mode."""


class JsonlCorruptError(JsonlError):
    """The last newline-terminated line is not a valid JSONL record."""


class JsonlSequenceError(JsonlError):
    """Missing, invalid, or non-consecutive source sequence number."""


class JsonlConflictError(JsonlError):
    """Replayed sequence has a different record than the persisted one."""


class export_json(object):

    def __init__(self, config=None):
        if config is None:
            config = {'verbose': False}

        self.config = config

        # last source sequence number confirmed as persisted (JSONL mode)
        self.last_sequence = None

    #
    # Json data
    #

    def process(self, parameters=None, data=None):
        if parameters is None:
            parameters = {}
        if data is None:
            data = {}

        # optional JSON Lines continuation mode
        if self._is_jsonl_mode(parameters):
            return self._process_jsonl(parameters=parameters, data=data)

        # if outputfile write json to file
        if 'outputfile' in parameters:

            import io
            with io.open(parameters['outputfile'], 'w', encoding='utf-8') as f:
                f.write(json.dumps(data, ensure_ascii=False))
        else:  # else print json
            print(json.dumps(data))

        return parameters, data

    #
    # JSONL lines are fsynced when they are written, so there is nothing left
    # to commit. Implemented so callers may use the exporter like the other
    # exporters, which are committed at the end of an ETL run.
    #
    def commit(self):
        pass

    #
    # Option handling
    #

    def _setting(self, parameters, names, default=None):
        """Return the first configured value for one of the alternative names.

        ETL connectors copy their config into parameters, so looking at both
        makes the options usable from config files and from direct API calls.
        """

        for name in names:
            if name in parameters:
                return parameters[name]

        for name in names:
            if name in self.config:
                return self.config[name]

        return default

    @staticmethod
    def _is_jsonl_value(value):
        if isinstance(value, str):
            return value.strip().lower() in JSONL_FORMATS
        return False

    @staticmethod
    def _is_true_value(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return False

    def _is_jsonl_mode(self, parameters):
        # An explicit False in parameters disables the mode, even if the
        # connector config enables it for a whole batch.
        if 'jsonl' in parameters and parameters['jsonl'] is False:
            return False

        value = self._setting(parameters, JSONL_ENABLE_OPTIONS, None)

        if isinstance(value, dict):
            # e.g. parameters['jsonl'] = {'sequence': 3}
            return True

        if self._is_true_value(value) or self._is_jsonl_value(value):
            return True

        value = self._setting(parameters, JSONL_FORMAT_OPTIONS, None)
        if self._is_jsonl_value(value):
            return True

        return False

    def _get_sequence(self, parameters):
        value = self._setting(parameters, SEQUENCE_OPTIONS, None)

        if value is None:
            # Also allow the mode option to carry the sequence number, i.e.
            # parameters['jsonl'] = 3 or {'sequence': 3, ...}.
            option = self._setting(parameters, JSONL_ENABLE_OPTIONS, None)

            if isinstance(option, dict):
                for name in SEQUENCE_OPTIONS:
                    if name in option:
                        value = option[name]
                        break
            elif isinstance(option, int) and not isinstance(option, bool):
                value = option

        if isinstance(value, bool):
            raise JsonlSequenceError(
                "Invalid sequence number for JSONL export: {!r}".format(value))

        # be tolerant if a config file stores the number as a string
        if isinstance(value, str):
            try:
                value = int(value.strip(), 10)
            except ValueError:
                raise JsonlSequenceError(
                    "Invalid sequence number for JSONL export: {!r}"
                    .format(value))

        if not isinstance(value, int):
            raise JsonlSequenceError(
                "Missing or invalid sequence number for JSONL export: {!r}"
                .format(value))

        if value < 1:
            raise JsonlSequenceError(
                "JSONL sequence numbers start at 1, got {}".format(value))

        return value

    def _tail_chunk_size(self):
        value = self.config.get(
            'jsonl_tail_chunk_size', DEFAULT_TAIL_CHUNK_SIZE)

        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return DEFAULT_TAIL_CHUNK_SIZE

        return value

    #
    # JSONL continuation
    #

    def _process_jsonl(self, parameters, data):
        sequence = self._get_sequence(parameters)

        outputfile = parameters.get('outputfile')
        if outputfile is None:
            outputfile = self.config.get('outputfile')

        if outputfile is None:
            # No file to resume from, but still emit a JSONL record.
            line = self._encode_line(sequence, data)
            sys.stdout.write(line.decode('utf-8'))
            sys.stdout.flush()
            confirmed = sequence
        else:
            confirmed = self._append_line(outputfile, sequence, data)

        # A successful return is the acknowledgement for the upstream: the
        # record has been persisted (and fsynced) at this point.
        self.last_sequence = confirmed

        return parameters, data

    def last_confirmed_sequence(self, outputfile):
        """Return the last source sequence number persisted in outputfile.

        Returns 0 if the file does not exist or contains no complete record.
        Only the end of the file is inspected (in chunks), so this can be used
        to find the position to resume from without reading the history.
        """

        path = os.fspath(outputfile)

        try:
            f = open(path, 'rb')
        except FileNotFoundError:
            return 0

        try:
            size = os.fstat(f.fileno()).st_size

            previous_newline, last_newline = self._find_last_record_bounds(
                f, size)

            if last_newline is None:
                return 0

            start = 0 if previous_newline is None else previous_newline + 1

            sequence, _ = self._read_record(f, path, start, last_newline)
        finally:
            f.close()

        return sequence

    @staticmethod
    def _encode_line(sequence, data):
        # Same serialization options as the default single document export
        # (UTF-8, non-ASCII characters kept as is), one line per record.
        line = json.dumps(
            {SEQUENCE_KEY: sequence, RECORD_KEY: data},
            ensure_ascii=False)

        return (line + '\n').encode('utf-8')

    def _append_line(self, outputfile, sequence, data):
        """Append one record or recognise it as already exported.

        Returns the sequence number that is confirmed as persisted.
        """

        path = os.fspath(outputfile)

        existed = os.path.exists(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)

        try:
            f = os.fdopen(fd, 'r+b')
        except BaseException:
            os.close(fd)
            raise

        confirmed = None

        try:
            size = os.fstat(f.fileno()).st_size

            previous_newline, last_newline = self._find_last_record_bounds(
                f, size)

            if last_newline is not None:
                line_start = 0 if previous_newline is None \
                    else previous_newline + 1

                # The last newline-terminated record decides the confirmed
                # position. If it is invalid, fail without touching the file.
                stored_sequence, stored_record = self._read_record(
                    f, path, line_start, last_newline)

                if sequence == stored_sequence:
                    if not self._records_equal(stored_record, data):
                        raise JsonlConflictError(
                            "JSONL conflict in file {}: sequence {} was "
                            "already exported with different content"
                            .format(path, sequence))

                    # Same record, already persisted: do not write it again.
                    # Sync again so that returning still means the record is
                    # durable, even if an earlier attempt failed after write.
                    self._truncate_tail_fragment(f, size, last_newline)
                    self._sync(f)
                    confirmed = stored_sequence

                elif sequence < stored_sequence:
                    # Older record: already covered by the export.
                    self._truncate_tail_fragment(f, size, last_newline)
                    confirmed = stored_sequence

                else:
                    expected = stored_sequence + 1
            else:
                # No complete record yet.
                expected = 1

            if confirmed is None:
                if sequence != expected:
                    raise JsonlSequenceError(
                        "JSONL sequence gap in file {}: expected {}, got {}"
                        .format(path, expected, sequence))

                # Serialize before touching the file, so a record that cannot
                # be encoded does not truncate a pending tail fragment.
                line = self._encode_line(sequence, data)

                # Everything is valid: drop an unfinished trailing fragment
                # and append the new record. The record is confirmed only
                # after the write has been persisted.
                end_of_records = self._truncate_tail_fragment(
                    f, size, last_newline)

                f.seek(end_of_records)
                f.write(line)
                self._sync(f)

                confirmed = sequence
        except BaseException:
            try:
                f.close()
            except OSError:
                pass

            # Do not leave an empty file behind when the very first record
            # was rejected before anything was written.
            if not existed:
                try:
                    if os.path.getsize(path) == 0:
                        os.unlink(path)
                except OSError:
                    pass

            raise
        else:
            f.close()

        if not existed:
            self._fsync_directory(path)

        return confirmed

    @staticmethod
    def _sync(f):
        f.flush()
        os.fsync(f.fileno())

    def _truncate_tail_fragment(self, f, size, last_newline):
        """Drop bytes after the last newline (an unfinished record).

        Returns the offset where the next record starts.
        """

        end_of_records = 0 if last_newline is None else last_newline + 1

        if size > end_of_records:
            f.truncate(end_of_records)
            self._sync(f)

        return end_of_records

    def _find_last_record_bounds(self, f, size):
        """Locate the last two newlines by scanning the tail in chunks.

        Returns (previous_newline, last_newline) as absolute byte offsets or
        (None, None) if the file contains no newline yet. Only the end of the
        file is read, never the whole history.
        """

        if size <= 0:
            return None, None

        chunk_size = self._tail_chunk_size()

        newline_positions = []
        position = size

        while position > 0 and len(newline_positions) < 2:
            read_size = min(chunk_size, position)
            position -= read_size

            f.seek(position)
            chunk = f.read(read_size)

            index = len(chunk)
            while len(newline_positions) < 2:
                index = chunk.rfind(b'\n', 0, index)
                if index < 0:
                    break
                newline_positions.append(position + index)

        if not newline_positions:
            return None, None

        last_newline = newline_positions[0]
        previous_newline = newline_positions[1] \
            if len(newline_positions) > 1 else None

        return previous_newline, last_newline

    def _read_record(self, f, path, start, end):
        """Read and validate the newline-terminated record in [start, end)."""

        f.seek(start)
        raw = f.read(end - start)

        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as e:
            raise JsonlCorruptError(
                "Invalid UTF-8 in last complete JSONL record of file {}: {}"
                .format(path, e))

        try:
            line = json.loads(text)
        except ValueError as e:
            raise JsonlCorruptError(
                "Invalid JSON in last complete record of file {}: {}"
                .format(path, e))

        if not isinstance(line, dict):
            raise JsonlCorruptError(
                "Last complete line of file {} is not a JSONL record object"
                .format(path))

        sequence = None
        for name in SEQUENCE_KEY_ALIASES:
            if name in line:
                sequence = line[name]
                break
        else:
            raise JsonlCorruptError(
                "Last complete line of file {} has no sequence number"
                .format(path))

        if isinstance(sequence, str):
            try:
                sequence = int(sequence.strip(), 10)
            except ValueError:
                sequence = None

        if isinstance(sequence, bool) or not isinstance(sequence, int) \
                or sequence < 1:
            raise JsonlCorruptError(
                "Invalid sequence number {!r} in last complete record of "
                "file {}".format(sequence, path))

        for name in RECORD_KEY_ALIASES:
            if name in line:
                return sequence, line[name]
        else:
            raise JsonlCorruptError(
                "Last complete line of file {} has no record".format(path))

    @staticmethod
    def _records_equal(stored, incoming):
        # Normalize through JSON so that a replay of the same record compares
        # equal even if the in-memory value differs in irrelevant ways
        # (e.g. tuples instead of lists or a different key order).
        try:
            normalized = json.loads(json.dumps(incoming, ensure_ascii=False))
        except (TypeError, ValueError):
            return False

        return stored == normalized

    @staticmethod
    def _fsync_directory(path):
        directory = os.path.dirname(os.path.abspath(path)) or '.'

        try:
            directory_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return

        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)
