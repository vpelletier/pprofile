#!/usr/bin/env python
# Copyright (C) 2013-2016  Vincent Pelletier <plr.vincent@gmail.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""
pprofile - Line-granularity, thread-aware deterministic and statistic
pure-python profiler

Usage as a command line:
$ pprofile --exclude-syspath some_python_executable arg1 ...
$ pprofile --exclude-syspath -m some_python_module -- arg1 ...
$ python -m pprofile --exclude-syspath some_python_executable arg1 ...
$ python -m pprofile -m some_python_module -- arg1 ...
See --help for all options.

Usage as a python module:

Deterministic profiling:
>>> prof = pprofile.Profile()
>>> with prof():
>>>     # Code to profile
>>> prof.print_stats()

Statistic profiling:
>>> prof = StatisticalProfile()
>>> with prof():
>>>     # Code to profile
>>> prof.print_stats()
"""
from __future__ import print_function, division
from collections import defaultdict, deque
from functools import partial, wraps
# Note: use time, not clock.
# Clock, at least on linux, ignores time not spent executing code
# (ex: time.sleep()). The goal of pprofile is not to profile python code
# execution as such (ie, to improve python interpreter), but to profile a
# possibly complex application, with its (IO) waits, sleeps, (...) so a
# developper can understand what is slow rather than what keeps the cpu busy.
# So using the wall-clock as a way to measure time spent is more meaningful.
# XXX: This said, if time() lacks precision, a better but likely
# platform-dependent wall-clock time source must be identified and used.
from time import time
from warnings import warn
import argparse
import io
import inspect
import linecache
import os
# not caught by 2to3, likely because pipes.quote is not documented in python 2
try:
    from pipes import quote as shlex_quote # Python 2
except ImportError:
    from shlex import quote as shlex_quote # Python 3
import platform
import re
import runpy
import shlex
from subprocess import list2cmdline as windows_list2cmdline
import sys
import threading
import zipfile
try:
    from IPython.core.magic import register_line_cell_magic
except ImportError:
    register_line_cell_magic = lambda x: x

__all__ = (
    'ProfileBase', 'ProfileRunnerBase', 'Profile', 'ThreadProfile',
    'StatisticProfile', 'StatisticThread', 'run', 'runctx', 'runfile',
    'runpath',
)
class BaseLineIterator(object):
    def __init__(self, getline, filename, global_dict):
        self._getline = getline
        self._filename = filename
        self._global_dict = global_dict
        self._lineno = 1

    def __iter__(self):
        return self

    def next(self):
        lineno = self._lineno
        self._lineno += 1
        return lineno, self._getline(self._filename, lineno, self._global_dict)

if sys.version_info < (3, ):
    import codecs
    # Find coding specification (see PEP-0263)
    _matchCoding = re.compile(
        r'^[ \t\f]*#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)',
    ).match
    class LineIterator(BaseLineIterator):
        def __init__(self, *args, **kw):
            super(LineIterator, self).__init__(*args, **kw)
            # Identify encoding.
            first_line = self._getline(self._filename, 1, self._global_dict)
            if isinstance(first_line, bytes):
                # BOM - python2 only detects the (discouraged) UTF-8 BOM
                if first_line.startswith(codecs.BOM_UTF8):
                    self._encoding = 'utf-8'
                else:
                    # PEP-0263: "the first or second line must match [_matchCoding]"
                    match = _matchCoding(first_line)
                    if match is None:
                        match = _matchCoding(
                            self._getline(self._filename, 2, self._global_dict),
                        )
                    if match is None:
                        self._encoding = 'ascii'
                    else:
                        self._encoding = match.group(1)
                self.next = self._next
            # else, first line is unicode, don't shadow superclass' "next".

        def _next(self):
            lineno, line = super(LineIterator, self).next()
            return lineno, line.decode(self._encoding)
else:
    # getline returns unicode objects, nothing to do
    LineIterator = BaseLineIterator

if platform.system() == 'Windows':
    quoteCommandline = windows_list2cmdline
else:
    def quoteCommandline(commandline):
        return ' '.join(shlex_quote(x) for x in commandline)

LINESEP = os.linesep
if isinstance(LINESEP, bytes):
    LINESEP = LINESEP.decode()

class EncodeOrReplaceWriter(object):
    """
    Write-only file-ish object which replaces unsupported chars when
    underlying file rejects them.
    """
    def __init__(self, out):
        self._encoding = out.encoding
        self._write = out.write

    def write(self, data):
        try:
            self._write(data)
        except UnicodeEncodeError:
            self._write(data.encode(
                self._encoding,
                errors='replace',
            ).decode(self._encoding))

def _getFuncOrFile(func, module, firstlineno):
    if func == '<module>':
        return module
    # Without involving firstlineno, cachegrind out has no way to distinguish
    # homonym functions within the same file.
    return '%s:%i' % (func, firstlineno)

def _isCallgrindName(filepath):
    return os.path.basename(filepath).startswith('cachegrind.out.')

class _FileTiming(object):
    """
    Accumulation of profiling statistics (line and call durations) for a given
    source "file" (unique global dict).

    Subclasses should be aware that:
    - this classes uses __slots__, mainly for cpu efficiency (property lookup
      is in a list instead of a dict)
    - it can access the BaseProfile instance which created any instace using
      the "profiler" property, should they share some state across source
      files.
    - methods on this class are profiling choke-point - keep customisations
      as cheap in CPU as you can !
    """
    __slots__ = ('line_dict', 'call_dict', 'filename', 'global_dict',
        'profiler')
    def __init__(self, filename, global_dict, profiler):
        self.filename = filename
        self.global_dict = global_dict
        self.line_dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        self.call_dict = {}
        # Note: not used in this implementation, may be used by subclasses.
        self.profiler = profiler

    def hit(self, code, line, duration):
        """
        A line has finished executing.

        code (code)
          container function's code object
        line (int)
          line number of just executed line
        duration (float)
          duration of the line, in seconds
        """
        entry = self.line_dict[line][code]
        entry[0] += 1
        entry[1] += duration

    def call(self, code, line, callee_file_timing, callee, duration, frame):
        """
        A call originating from this file returned.

        code (code)
          caller's code object
        line (int)
          caller's line number
        callee_file_timing (FileTiming)
          callee's FileTiming
        callee (code)
          callee's code object
        duration (float)
          duration of the call, in seconds
        frame (frame)
          calle's entire frame as of its return
        """
        try:
            entry = self.call_dict[(code, line, callee)]
        except KeyError:
            self.call_dict[(code, line, callee)] = [callee_file_timing, 1, duration]
        else:
            entry[1] += 1
            entry[2] += duration

    def getHitStatsFor(self, line):
        for code, (hits, duration) in self.line_dict.get(line, {None: (0, 0)}).iteritems():
            if code is None:
                firstlineno = None
            else:
                firstlineno = code.co_firstlineno
                code = code.co_name
            yield code, firstlineno, hits, duration

    def getCallListByLine(self):
        result = defaultdict(list)
        for (code, line, callee), (callee_file_timing, hit, duration) in \
                self.call_dict.iteritems():
            result[line].append((
                code.co_name, code.co_firstlineno,
                hit, duration,
                callee_file_timing.filename, callee.co_firstlineno, callee.co_name,
            ))
        return result

    def getTotalTime(self):
        return sum(
            y[1]
            for x in self.line_dict.itervalues()
            for y in x.itervalues()
        )

    def getTotalHitCount(self):
        return sum(
            y[0]
            for x in self.line_dict.itervalues()
            for y in x.itervalues()
        )

    def getSortKey(self):
        # total duration first, then total hit count for statistical profiling
        result = [0, 0]
        for entry in self.line_dict.itervalues():
            for hit, duration in entry.itervalues():
                result[0] += duration
                result[1] += hit
        return result

FileTiming = _FileTiming

class LocalDescriptor(threading.local):
    """
    Implementation of descriptor API for thread-local properties.
    """
    def __init__(self, func=None):
        """
        func (callable)
          If provided, called when a missing property is accessed
          (ex: accessing thread never initialised that property).
          If None, AttributeError is raised.
        """
        super(LocalDescriptor, self).__init__()
        if func is not None:
            self.func = func

    def __get__(self, instance, owner):
        try:
            return getattr(self, str(id(instance)))
        except AttributeError:
            # Raises AttributeError if func was not provided.
            value = self.func()
            setattr(self, str(id(instance)), value)
            return value

    def __set__(self, instance, value):
        setattr(self, str(id(instance)), value)

    def __delete__(self, instance):
        try:
            delattr(self, str(id(instance)))
        except AttributeError:
            pass

_ANNOTATE_HEADER = \
    u'%6s|%10s|' \
    u'%13s|%13s|%7s|' \
    u'Source code' % (
        u'Line #', u'Hits',
        u'Time', u'Time per hit', u'%',
    )
_ANNOTATE_HORIZONTAL_LINE = u''.join(x == u'|' and u'+' or u'-'
    for x in _ANNOTATE_HEADER)
_ANNOTATE_FORMAT = \
    u'%(lineno)6i|%(hits)10i|' \
    u'%(time)13g|%(time_per_hit)13g|%(percent)6.2f%%|' \
    u'%(line)s'
_ANNOTATE_CALL_FORMAT = \
    u'(call)|%(hits)10i|' \
    u'%(time)13g|%(time_per_hit)13g|%(percent)6.2f%%|' \
    u'# %(callee_file)s:%(callee_line)s %(callee_name)s'

def _initStack():
    # frame_time: when current frame execution started/resumed last
    # frame_discount: time discounted from current frame, because it appeared
    #   lower in the call stack from the same callsite
    # lineno: latest line which execution started
    # line_time: time at which latest line started being executed
    # line_duration: total time spent in current line up to last resume
    now = time()
    return (deque([[now, 0, None, now, 0]]), defaultdict(deque))

def _verboseProfileDecorator(self):
    def decorator(func):
        @wraps(func)
        def wrapper(frame, event, arg):
            self._traceEvent(frame, event)
            return func(frame, event, arg)
        return wrapper
    return decorator

class ProfileBase(object):
    """
    Methods common to deterministic and statistic profiling.

    Subclasses can override the "FileTiming" property to use a different class.
    """
    __slots__ = (
        'file_dict',
        'global_dict',
        'total_time',
        '__dict__',
        '__weakref__',
    )
    FileTiming = _FileTiming

    def __init__(self):
        self.file_dict = {}
        self.global_dict = {}
        self.total_time = 0

    def _getFileTiming(self, frame):
        try:
            return self.global_dict[frame.f_code]
        except KeyError:
            f_globals = frame.f_globals
            name = self._getFilename(frame)
            try:
                file_timing = self.file_dict[name]
            except KeyError:
                self.file_dict[name] = file_timing = self.FileTiming(
                    name,
                    f_globals,
                    self,
                )
            self.global_dict[frame.f_code] = file_timing
            return file_timing

    @staticmethod
    def _getFilename(frame):
        """
        Overload in subclasses to customise filename generation.
        """
        return frame.f_code.co_filename

    @staticmethod
    def _getline(filename, lineno, global_dict):
        """
        Overload in subclasses to customise source retrieval.
        """
        return linecache.getline(filename, lineno, global_dict)

    def getFilenameSet(self):
        """
        Returns a set of profiled file names.

        Note: "file name" is used loosely here. See python documentation for
        co_filename, linecache module and PEP302. It may not be a valid
        filesystem path.
        """
        result = set(self.file_dict)
        # Ignore profiling code. __file__ does not always provide consistent
        # results with f_code.co_filename (ex: easy_install with zipped egg),
        # so inspect current frame instead.
        # Get current file from one of pprofile methods. Compatible with
        # implementations that do not have the inspect.currentframe() method
        # (e.g. IronPython).
        # XXX: Assumes that all of pprofile code is in a single file.
        # XXX: Assumes that _initStack exists in pprofile module.
        result.discard(inspect.getsourcefile(_initStack))
        return result

    def _getFileNameList(self, filename, may_sort=True):
        if filename is None:
            filename = self.getFilenameSet()
        elif isinstance(filename, basestring):
            return [filename]
        if may_sort:
            try:
                # Detect if filename is an ordered data type.
                filename[:0]
            except TypeError:
                # Not ordered, sort.
                file_dict = self.file_dict
                filename = sorted(filename, reverse=True,
                    key=lambda x: file_dict[x].getSortKey()
                )
        return filename

    def _iterFile(self, name, call_list_by_line):
        if call_list_by_line:
            last_call_line = max(call_list_by_line)
        else:
            last_call_line = 0
        file_timing = self.file_dict[name]
        for lineno, line in LineIterator(
            self._getline,
            file_timing.filename,
            file_timing.global_dict,
        ):
            for func, firstlineno, hits, duration in file_timing.getHitStatsFor(
                lineno):
                if func is None:
                    # In case the line has no hit but has a call (happens in
                    # statistical profiling, as hits are on leaves only).
                    # func & firstlineno are expected to be constant on a
                    # given line (accumulated data is redundant)
                    call_list = call_list_by_line.get(lineno)
                    if call_list:
                        func, firstlineno = call_list[0][:2]
                if not line and lineno > last_call_line:
                    if hits == 0:
                        break
                    # Line exists in stats, but not in file. Happens on 1st
                    # line of empty files (ex: __init__.py). Fake the presence
                    # of an empty line.
                    line = LINESEP
                yield lineno, func, firstlineno, hits, duration, line
            else:
                continue
            break

    def callgrind(self, out, filename=None, commandline=None, relative_path=False):
        """
        Dump statistics in callgrind format.
        Contains:
        - per-line hit count, time and time-per-hit
        - call associations (call tree)
          Note: hit count is not inclusive, in that it is not the sum of all
          hits inside that call.
        Time unit: microsecond (1e-6 second).
        out (file-ish opened for writing)
            Destination of callgrind profiling data.
        filename (str, collection of str)
            If provided, dump stats for given source file(s) only.
            By default, list for all known files.
        commandline (anything with __str__)
            If provided, will be output as the command line used to generate
            this profiling data.
        relative_path (bool)
            When True, absolute elements are stripped from path. Useful when
            maintaining several copies of source trees with their own
            profiling result, so kcachegrind does not look in system-wide
            files which may not match with profiled code.
        """
        print(u'# callgrind format', file=out)
        print(u'version: 1', file=out)
        print(u'creator: pprofile', file=out)
        print(u'event: usphit :microseconds/hit', file=out)
        print(u'events: hits microseconds usphit', file=out)
        if commandline is not None:
            print(u'cmd:', commandline, file=out)
        file_dict = self.file_dict
        if relative_path:
            convertPath = _relpath
        else:
            convertPath = lambda x: x
        if os.path.sep != "/":
            # qCacheGrind (windows build) needs at least one UNIX separator
            # in path to find the file. Adapt here even if this is probably
            # more of a qCacheGrind issue...
            convertPath = lambda x, cascade=convertPath: cascade(
                '/'.join(x.split(os.path.sep))
            )
        for current_file in self._getFileNameList(filename, may_sort=False):
            call_list_by_line = file_dict[current_file].getCallListByLine()
            print(u'fl=%s' % convertPath(current_file), file=out)
            # When a local callable is created an immediately executed, this
            # loop would start a new "fn=" section but would not end it before
            # emitting "cfn=" lines, making the callee appear as not being
            # called by interrupted "fn=" section.
            # So dispatch all functions in a first pass, and build
            # uninterrupted sections in a second pass.
            # Note: cost line is a list just to be mutable. A single item is
            # expected.
            func_dict = defaultdict(lambda: defaultdict(lambda: ([], [])))
            for lineno, func, firstlineno, hits, duration, _ in self._iterFile(
                    current_file, call_list_by_line):
                call_list = call_list_by_line.get(lineno, ())
                if not hits and not call_list:
                    continue
                ticks = int(duration * 1000000)
                if hits == 0:
                    ticksperhit = 0
                else:
                    ticksperhit = ticks // hits
                func_dict[(func, firstlineno)][lineno][0].append(
                    u'%i %i %i %i' % (lineno, hits, ticks, ticksperhit),
                )
                for (
                    caller_func, caller_firstlineno,
                    call_hits, call_duration,
                    callee_file, callee_line, callee_func,
                ) in sorted(call_list, key=lambda x: x[2:4]):
                    call_ticks = int(call_duration * 1000000)
                    func_call_list = func_dict[
                        (caller_func, caller_firstlineno)
                    ][lineno][1]
                    append = func_call_list.append
                    if callee_file != current_file:
                        append(u'cfl=%s' % convertPath(callee_file))
                    append(u'cfn=%s' % _getFuncOrFile(callee_func, callee_file, callee_line))
                    append(u'calls=%i %i' % (call_hits, callee_line))
                    append(u'%i %i %i %i' % (lineno, call_hits, call_ticks, call_ticks // call_hits))
            for (func, firstlineno), line_dict in func_dict.iteritems():
                print(u'fn=%s' % _getFuncOrFile(func, current_file, firstlineno), file=out)
                for lineno, (func_hit_list, func_call_list) in sorted(line_dict.iteritems()):
                    if func_hit_list:
                        line, = func_hit_list
                        print(line, file=out)
                    for line in func_call_list:
                        print(line, file=out)

    def annotate(self, out, filename=None, commandline=None, relative_path=False):
        """
        Dump annotated source code with current profiling statistics to "out"
        file.
        Time unit: second.
        out (file-ish opened for writing)
            Destination of annotated sources.
        filename (str, collection of str)
            If provided, dump stats for given source file(s) only.
            If unordered collection, it will get sorted by decreasing total
            file score (total time if available, then total hit count).
            By default, list for all known files.
        commandline (anything with __str__)
            If provided, will be output as the command line used to generate
            this annotation.
        relative_path (bool)
            For compatibility with callgrind. Ignored.
        """
        file_dict = self.file_dict
        total_time = self.total_time
        if commandline is not None:
            print(u'Command line:', commandline, file=out)
        print(u'Total duration: %gs' % total_time, file=out)
        if not total_time:
            return
        def percent(value, scale):
            if scale == 0:
                return 0
            return value * 100 / scale
        for name in self._getFileNameList(filename):
            file_timing = file_dict[name]
            file_total_time = file_timing.getTotalTime()
            call_list_by_line = file_timing.getCallListByLine()
            print(u'File: %s' % name, file=out)
            print(u'File duration: %gs (%.2f%%)' % (file_total_time,
                percent(file_total_time, total_time)), file=out)
            print(_ANNOTATE_HEADER, file=out)
            print(_ANNOTATE_HORIZONTAL_LINE, file=out)
            for lineno, _, _, hits, duration, line in self._iterFile(name,
                    call_list_by_line):
                if hits:
                    time_per_hit = duration / hits
                else:
                    time_per_hit = 0
                print(_ANNOTATE_FORMAT % {
                    u'lineno': lineno,
                    u'hits': hits,
                    u'time': duration,
                    u'time_per_hit': time_per_hit,
                    u'percent': percent(duration, total_time),
                    u'line': line.rstrip(),
                }, file=out)
                for (
                    _, _,
                    call_hits, call_duration,
                    callee_file, callee_line, callee_name,
                ) in call_list_by_line.get(lineno, ()):
                    print(_ANNOTATE_CALL_FORMAT % {
                        u'hits': call_hits,
                        u'time': call_duration,
                        u'time_per_hit': call_duration / call_hits,
                        u'percent': percent(call_duration, total_time),
                        u'callee_file': callee_file,
                        u'callee_line': callee_line,
                        u'callee_name': callee_name,
                    }, file=out)

    def _iterRawFile(self, name):
        lineno = 0
        file_timing = self.file_dict[name]
        while True:
            lineno += 1
            line = self._getline(file_timing.filename, lineno,
                file_timing.global_dict)
            if not line:
                break
            yield line

    def iterSource(self):
        """
        Iterator over all involved files.
        Yields 2-tuple composed of file path and an iterator over
        (non-annotated) source lines.

        Can be used to generate a file tree for use with kcachegrind, for
        example.
        """
        for name in self._getFileNameList(None):
            yield name, self._iterRawFile(name)

    # profile/cProfile-like API
    def dump_stats(self, filename):
        """
        Similar to profile.Profile.dump_stats - but different output format !
        """
        if _isCallgrindName(filename):
            with open(filename, 'w') as out:
                self.callgrind(out)
        else:
            with io.open(filename, 'w', errors='replace') as out:
                self.annotate(out)

    def print_stats(self):
        """
        Similar to profile.Profile.print_stats .
        Returns None.
        """
        self.annotate(EncodeOrReplaceWriter(sys.stdout))

class ProfileRunnerBase(object):
    def __call__(self):
        return self

    def __enter__(self):
        raise NotImplementedError

    def __exit__(self, exc_type, exc_val, exc_tb):
        raise NotImplementedError

    # profile/cProfile-like API
    def runctx(self, cmd, globals, locals):
        """Similar to profile.Profile.runctx ."""
        with self():
            exec(cmd, globals, locals)
        return self

    def runcall(self, func, *args, **kw):
        """Similar to profile.Profile.runcall ."""
        with self():
            return func(*args, **kw)

    def runfile(self, fd, argv, fd_name='<unknown>', compile_flags=0,
            dont_inherit=1, globals={}):
        with fd:
            code = compile(fd.read(), fd_name, 'exec', flags=compile_flags,
                dont_inherit=dont_inherit)
        original_sys_argv = list(sys.argv)
        ctx_globals = globals.copy()
        ctx_globals['__file__'] = fd_name
        ctx_globals['__name__'] = '__main__'
        ctx_globals['__package__'] = None
        try:
            sys.argv[:] = argv
            return self.runctx(code, ctx_globals, None)
        finally:
            sys.argv[:] = original_sys_argv

    def runpath(self, path, argv):
        original_sys_path = list(sys.path)
        try:
            sys.path.insert(0, os.path.dirname(path))
            return self.runfile(open(path, 'rb'), argv, fd_name=path)
        finally:
            sys.path[:] = original_sys_path

    def runmodule(self, module, argv):
        original_sys_argv = list(sys.argv)
        original_sys_path0 = sys.path[0]
        try:
            sys.path[0] = os.getcwd()
            sys.argv[:] = argv
            with self():
                runpy.run_module(module, run_name='__main__', alter_sys=True)
        finally:
            sys.argv[:] = original_sys_argv
            sys.path[0] = original_sys_path0
        return self

class Profile(ProfileBase, ProfileRunnerBase):
    """
    Deterministic, recursive, line-granularity, profiling class.

    Does not require any source code change to work.
    If the performance hit is too large, it can benefit from some
    integration (calling enable/disable around selected code chunks).

    The sum of time spent in all profiled lines is less than the total
    profiled time reported. This is (part of) profiling overhead.
    This also mans that sum of time-spent-on-line percentage is less than 100%.

    All times are "internal time", ie they do not count time spent inside
    called (profilable, so python) functions.
    """
    __slots__ = (
        '_global_trace',
        '_local_trace',
        'stack',
        'enabled_start',
    )

    def __init__(self, verbose=False):
        super(Profile, self).__init__()
        if verbose:
            self._global_trace = _verboseProfileDecorator(self)(
                self._real_global_trace)
            self._local_trace = _verboseProfileDecorator(self)(
                self._real_local_trace)
        else:
            self._global_trace = self._real_global_trace
            self._local_trace = self._real_local_trace
        self.enabled_start = None

    def _enable(self):
        """
        Overload this method when subclassing. Called before actually
        enabling trace.
        """
        try:
            self.stack
        except AttributeError:
            # In case subclass declared stack as descriptor.
            self.stack = _initStack()
        self.enabled_start = time()

    def enable(self):
        """
        Enable profiling.
        """
        if self.enabled_start:
            warn('Duplicate "enable" call')
        else:
            self._enable()
            sys.settrace(self._global_trace)

    def _disable(self):
        """
        Overload this method when subclassing. Called after actually disabling
        trace.
        """
        self.total_time += time() - self.enabled_start
        self.enabled_start = None
        del self.stack

    def disable(self):
        """
        Disable profiling.
        """
        if self.enabled_start:
            sys.settrace(None)
            self._disable()
        else:
            warn('Duplicate "disable" call')

    def __enter__(self):
        """
        __enter__() -> self
        """
        self.enable()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        __exit__(*excinfo) -> None. Disables profiling.
        """
        self.disable()

    def _traceEvent(self, frame, event):
        f_code = frame.f_code
        lineno = frame.f_lineno
        print('%10.6f%s%s %s:%s %s+%s' % (
            time() - self.enabled_start,
            ' ' * len(self.stack[0]),
            event,
            f_code.co_filename,
            lineno,
            f_code.co_name,
            lineno - f_code.co_firstlineno,
        ), file=sys.stderr)

    def _real_global_trace(self, frame, event, arg):
        local_trace = self._local_trace
        if local_trace is not None:
            event_time = time()
            callee_entry = [event_time, 0, frame.f_lineno, event_time, 0]
            stack, callee_dict = self.stack
            try:
                caller_entry = stack[-1]
            except IndexError:
                pass
            else:
                # Suspend caller frame
                frame_time, frame_discount, lineno, line_time, line_duration = caller_entry
                caller_entry[4] = event_time - line_time + line_duration
                callee_dict[(frame.f_back.f_code, frame.f_code)].append(callee_entry)
            stack.append(callee_entry)
        return local_trace

    def _real_local_trace(self, frame, event, arg):
        if event == 'line' or event == 'return':
            event_time = time()
            stack, callee_dict = self.stack
            try:
                stack_entry = stack[-1]
            except IndexError:
                warn('Profiling stack underflow, disabling.')
                self.disable()
                return None
            frame_time, frame_discount, lineno, line_time, line_duration = stack_entry
            self._getFileTiming(frame).hit(frame.f_code, lineno,
                event_time - line_time + line_duration)
            if event == 'line':
                # Start a new line
                stack_entry[2] = frame.f_lineno
                stack_entry[3] = event_time
                stack_entry[4] = 0
            else:
                # 'return' event, <frame> is still callee
                # Resume caller frame
                stack.pop()
                stack[-1][3] = event_time
                caller_frame = frame.f_back
                caller_code = caller_frame.f_code
                callee_code = frame.f_code
                callee_entry_list = callee_dict[(caller_code, callee_code)]
                callee_entry_list.pop()
                call_duration = event_time - frame_time
                if callee_entry_list:
                    # Callee is also somewhere up the stack, so discount this
                    # call duration from it.
                    callee_entry_list[-1][1] += call_duration
                self._getFileTiming(caller_frame).call(
                    caller_code, caller_frame.f_lineno,
                    self._getFileTiming(frame),
                    callee_code, call_duration - frame_discount,
                    frame,
                )
        return self._local_trace

    # profile/cProfile-like API
    def run(self, cmd):
        """Similar to profile.Profile.run ."""
        import __main__
        dikt = __main__.__dict__
        return self.runctx(cmd, dikt, dikt)

class ThreadProfile(Profile):
    """
    threading.Thread-aware version of Profile class.

    Threads started after enable() call will be profiled.
    After disable() call, threads will need to be switched into and trigger a
    trace event (typically a "line" event) before they can notice the
    disabling.
    """
    __slots__ = ('_local_trace_backup', )

    stack = LocalDescriptor(_initStack)

    def __init__(self, **kw):
        super(ThreadProfile, self).__init__(**kw)
        # XXX: file_dict, global_dict are not thread safe ! (no problem for total_time ?)
        self._local_trace_backup = self._local_trace

    def _enable(self):
        self._local_trace = self._local_trace_backup
        threading.settrace(self._global_trace)
        super(ThreadProfile, self)._enable()

    def _disable(self):
        super(ThreadProfile, self)._disable()
        threading.settrace(None)
        self._local_trace = None

class StatisticProfile(ProfileBase, ProfileRunnerBase):
    """
    Statistic profiling class.

    This class does not gather its own samples by itself.
    Instead, it must be provided with call stacks (as returned by
    sys._getframe() or sys._current_frames()).
    """
    def __init__(self):
        super(StatisticProfile, self).__init__()
        self.total_time = 1

    def sample(self, frame):
        getFileTiming = self._getFileTiming
        called_timing = getFileTiming(frame)
        called_code = frame.f_code
        called_timing.hit(called_code, frame.f_lineno, 0)
        while True:
            caller = frame.f_back
            if caller is None:
                break
            caller_timing = getFileTiming(caller)
            caller_code = caller.f_code
            caller_timing.call(caller_code, caller.f_lineno,
                called_timing, called_code, 0, frame)
            called_timing = caller_timing
            frame = caller
            called_code = caller_code

    def __call__(self, period=.001, single=True, group=None, name=None):
        """
        Instanciate StatisticThread.

        >>> s_profile = StatisticProfile()
        >>> with s_profile(single=False):
        >>>    # Code to profile
        Is equivalent to:
        >>> s_profile = StatisticProfile()
        >>> s_thread = StatisticThread(profiler=s_profile, single=False)
        >>> with s_thread:
        >>>    # Code to profile
        """
        return StatisticThread(
            profiler=self, period=period, single=single, group=group,
            name=name,
        )

# BBB
StatisticalProfile = StatisticProfile

class StatisticThread(threading.Thread, ProfileRunnerBase):
    """
    Usage in a nutshell:
      with StatisticThread() as profiler_thread:
        # do stuff
      profiler_thread.profiler.print_stats()
    """
    __slots__ = (
        '_test',
        '_start_time',
        'clean_exit',
    )

    def __init__(self, profiler=None, period=.001, single=True, group=None, name=None):
        """
        profiler (None or StatisticProfile instance)
          Available on instances as the "profiler" read-only property.
          If None, a new profiler instance will be created.
        period (float)
          How many seconds to wait between consecutive samples.
          The smaller, the more profiling overhead, but the faster results
          become meaningful.
          The larger, the less profiling overhead, but requires long profiling
          session to get meaningful results.
        single (bool)
          Profile only the thread which created this instance.
        group, name
          See Python's threading.Thread API.
        """
        if profiler is None:
            profiler = StatisticProfile()
        if single:
            self._test = lambda x, ident=threading.current_thread().ident: ident == x
        else:
            self._test = None
        super(StatisticThread, self).__init__(
            group=group,
            name=name,
        )
        self._stop_event = threading.Event()
        self._period = period
        self._profiler = profiler
        profiler.total_time = 0
        self.daemon = True
        self.clean_exit = False

    @property
    def profiler(self):
        return self._profiler

    def start(self):
        self.clean_exit = False
        self._can_run = True
        self._start_time = time()
        super(StatisticThread, self).start()

    def stop(self):
        """
        Request thread to stop.
        Does not wait for actual termination (use join() method).
        """
        if self.is_alive():
            self._can_run = False
            self._stop_event.set()
            self._profiler.total_time += time() - self._start_time
            self._start_time = None

    def __enter__(self):
        """
        __enter__() -> self
        """
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        __exit__(*excinfo) -> None. Stops and joins profiling thread.
        """
        self.stop()
        self.join()

    def run(self):
        current_frames = sys._current_frames
        test = self._test
        if test is None:
            test = lambda x, ident=threading.current_thread().ident: ident != x
        sample = self._profiler.sample
        stop_event = self._stop_event
        wait = partial(stop_event.wait, self._period)
        while self._can_run:
            for ident, frame in current_frames().iteritems():
                if test(ident):
                    sample(frame)
            frame = None
            wait()
        stop_event.clear()
        self.clean_exit = True

    def callgrind(self, *args, **kw):
        warn('deprecated', DeprecationWarning)
        return self._profiler.callgrind(*args, **kw)

    def annotate(self, *args, **kw):
        warn('deprecated', DeprecationWarning)
        return self._profiler.annotate(*args, **kw)

    def dump_stats(self, *args, **kw):
        warn('deprecated', DeprecationWarning)
        return self._profiler.dump_stats(*args, **kw)

    def print_stats(self, *args, **kw):
        warn('deprecated', DeprecationWarning)
        return self._profiler.print_stats(*args, **kw)

    def iterSource(self, *args, **kw):
        warn('deprecated', DeprecationWarning)
        return self._profiler.iterSource(*args, **kw)

# BBB
StatisticalThread = StatisticThread

# profile/cProfile-like API (no sort parameter !)
def _run(threads, verbose, func_name, filename, *args, **kw):
    if threads:
        klass = ThreadProfile
    else:
        klass = Profile
    prof = klass(verbose=verbose)
    try:
        try:
            getattr(prof, func_name)(*args, **kw)
        except SystemExit:
            pass
    finally:
        if filename is None:
            prof.print_stats()
        else:
            prof.dump_stats(filename)

def run(cmd, filename=None, threads=True, verbose=False):
    """Similar to profile.run ."""
    _run(threads, verbose, 'run', filename, cmd)

def runctx(cmd, globals, locals, filename=None, threads=True, verbose=False):
    """Similar to profile.runctx ."""
    _run(threads, verbose, 'runctx', filename, cmd, globals, locals)

def runfile(fd, argv, fd_name='<unknown>', compile_flags=0, dont_inherit=1,
        filename=None, threads=True, verbose=False):
    """
    Run code from given file descriptor with profiling enabled.
    Closes fd before executing contained code.
    """
    _run(threads, verbose, 'runfile', filename, fd, argv, fd_name,
        compile_flags, dont_inherit)

def runpath(path, argv, filename=None, threads=True, verbose=False):
    """
    Run code from open-accessible file path with profiling enabled.
    """
    _run(threads, verbose, 'runpath', filename, path, argv)

_allsep = os.sep + (os.altsep or '')

def _relpath(name):
    """
    Strip absolute components from path.
    Inspired from zipfile.write().
    """
    return os.path.normpath(os.path.splitdrive(name)[1]).lstrip(_allsep)

def _main(argv, stdin=None):
    format_dict = {
        'text': 'annotate',
        'callgrind': 'callgrind',
    }

    parser = argparse.ArgumentParser(argv[0])
    parser.add_argument('script', help='Python script to execute (optionaly '
        'followed by its arguments)', nargs='?')
    parser.add_argument('argv', nargs=argparse.REMAINDER)
    parser.add_argument('-o', '--out', default='-',
        help='Write annotated sources to this file. Defaults to stdout.')
    parser.add_argument('-z', '--zipfile',
        help='Name of a zip file to generate from all involved source files. '
        'Useful with callgrind output.')
    parser.add_argument('-t', '--threads', default=1, type=int, help='If '
        'non-zero, trace threads spawned by program. Default: %(default)s')
    parser.add_argument('-f', '--format', choices=format_dict,
        help='Format in which output is generated. If not set, auto-detected '
        'from filename if provided, falling back to "text".')
    parser.add_argument('-v', '--verbose', action='store_true',
        help='Enable profiler internal tracing output. Cryptic and verbose.')
    parser.add_argument('-s', '--statistic', default=0, type=float,
        help='Use this period for statistic profiling, or use deterministic '
        'profiling when 0.')
    parser.add_argument('-m', dest='module',
        help='Searches sys.path for the named module and runs the '
        'corresponding .py file as a script. When given, positional arguments '
        'become sys.argv[1:]')

    group = parser.add_argument_group(
        title='Filtering',
        description='Allows excluding (and re-including) code from '
            '"file names" matching regular expressions. '
            '"file name" follows the semantics of python\'s "co_filename": '
            'it may be a valid path, of an existing or non-existing file, '
            'but it may be some arbitrary string too.'
    )
    group.add_argument('--exclude-syspath', action='store_true',
        help='Exclude all from default "sys.path". Beware: this will also '
        'exclude properly-installed non-standard modules, which may not be '
        'what you want.')
    group.add_argument('--exclude', action='append', default=[],
        help='Exclude files whose name starts with any pattern.')
    group.add_argument('--include', action='append', default=[],
        help='Include files whose name would have otherwise excluded. '
        'If no exclusion was specified, all paths are excluded first.')

    options = parser.parse_args(argv[1:])
    if options.exclude_syspath:
        options.exclude.extend('^' + re.escape(x) for x in sys.path)
    if options.include and not options.exclude:
        options.exclude.append('') # All-matching regex
    if options.verbose:
        if options.exclude:
            print('Excluding:', file=sys.stderr)
            for regex in options.exclude:
                print('\t' + regex, file=sys.stderr)
            if options.include:
                print('But including:', file=sys.stderr)
                for regex in options.include:
                    print('\t' + regex, file=sys.stderr)

    if options.module is None:
        if options.script is None:
            parser.error('too few arguments')
        args = [options.script] + options.argv
        runner_method_kw = {
            'path': args[0],
            'argv': args,
        }
        runner_method_id = 'runpath'
    elif stdin is not None and options.module == '-':
        # Undocumented way of using -m, used internaly by %%pprofile
        args = ['<stdin>']
        if options.script is not None:
            args.append(options.script)
        args.extend(options.argv)
        import __main__
        runner_method_kw = {
            'fd': stdin,
            'argv': args,
            'fd_name': '<stdin>',
            'globals': __main__.__dict__,
        }
        runner_method_id = 'runfile'
    else:
        args = [options.module]
        if options.script is not None:
            args.append(options.script)
        args.extend(options.argv)
        runner_method_kw = {
            'module': options.module,
            'argv': args,
        }
        runner_method_id = 'runmodule'
    if options.format is None:
        if _isCallgrindName(options.out):
            options.format = 'callgrind'
        else:
            options.format = 'text'
    relative_path = options.format == 'callgrind' and options.zipfile
    if options.statistic:
        prof = StatisticalProfile()
        runner = StatisticalThread(
            profiler=prof,
            period=options.statistic,
            single=not options.threads,
        )
    else:
        if options.threads:
            klass = ThreadProfile
        else:
            klass = Profile
        prof = runner = klass(verbose=options.verbose)
    try:
        getattr(runner, runner_method_id)(**runner_method_kw)
    finally:
        if options.out == '-':
            out = EncodeOrReplaceWriter(sys.stdout)
            close = lambda: None
        else:
            out = io.open(options.out, 'w', errors='replace')
            close = out.close
        if options.exclude:
            exclusion_search_list = [
                re.compile(x).search for x in options.exclude
            ]
            include_search_list = [
                re.compile(x).search for x in options.include
            ]
            filename_set = {
                x for x in prof.getFilenameSet()
                if not (
                    any(y(x) for y in exclusion_search_list) and
                    not any(y(x) for y in include_search_list)
                )
            }
        else:
            filename_set = None
        commandline = quoteCommandline(args)
        getattr(prof, format_dict[options.format])(
            out,
            filename=filename_set,
            # python2 repr returns bytes, python3 repr returns unicode
            commandline=getattr(
                commandline,
                'decode',
                lambda _: commandline,
            )('ascii'),
            relative_path=relative_path,
        )
        close()
        zip_path = options.zipfile
        if zip_path:
            if relative_path:
                convertPath = _relpath
            else:
                convertPath = lambda x: x
            with zipfile.ZipFile(
                        zip_path,
                        mode='w',
                        compression=zipfile.ZIP_DEFLATED,
                    ) as zip_file:
                for name, lines in prof.iterSource():
                    zip_file.writestr(
                        convertPath(name),
                        ''.join(lines)
                    )
    if options.statistic and not runner.clean_exit:
        # Mostly useful for regresion testing, as exceptions raised in threads
        # do not change exit status.
        sys.exit(1)

def pprofile(line, cell=None):
    """
    Profile line execution.
    """
    if cell is None:
        # TODO: detect and use arguments (statistical profiling, ...) ?
        return run(line)
    return _main(
        ['%%pprofile', '-m', '-'] + shlex.split(line),
        io.StringIO(cell),
    )
try:
    register_line_cell_magic(pprofile)
except Exception:
    # ipython can be imported, but may not be currently running.
    pass
del pprofile

def main():
    _main(sys.argv)

if __name__ == '__main__':
    main()
