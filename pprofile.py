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
from __future__ import print_function
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
import inspect
import linecache
import os
import re
import runpy
import sys
import threading
import zipfile

__all__ = (
    'ProfileBase', 'ProfileRunnerBase', 'Profile', 'ThreadProfile',
    'StatisticProfile', 'StatisticThread', 'run', 'runctx', 'runfile',
    'runpath',
)

if sys.version_info < (3, ):
    # Python 2.x linecache returns non-decoded strings, which cause errors when
    # mixing source code of different encodings and writing to a fixed-encoding
    # output. So instead of writing a lot of code to properly handle this, just
    # emit text the Python 2 way: don't specify encoding.
    def _open(name, mode, errors):
        return open(name, mode)

    def _reopen(stream, encoding=None, errors='strict'):
        return stream
else:
    import codecs
    _open = open

    def _reopen(stream, encoding=None, errors='strict'):
        """
        Reopen given stream, optionally changing the encoding and error handler.
        """
        if encoding is None:
            encoding = stream.encoding
        # XXX: Python3 < 3.2 and ipykernel.iostream.OutStream at least up to
        # 4.5.0 do not have stream.buffer.
        # I do not see a way to change errors without also potentially changing
        # the encoding, and there does not seem to be a way to change encoding
        # without having to access the binary stream.
        try:
            buf = stream.buffer
        except AttributeError:
            warn(
                'Cannot access "%r.buffer", invalid entities from source '
                'files will cause errors when annotating.' % (stream, )
            )
            return stream
        return codecs.getwriter(encoding)(buf, errors=errors)

def _getFuncOrFile(func, module, line):
    if func == '<module>' or func is None:
        return module
    else:
        return '%s:%s' % (func, line)

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
        'raw_filename', 'profiler')
    def __init__(self, raw_filename, filename, global_dict, profiler):
        self.raw_filename = raw_filename
        self.filename = filename
        self.global_dict = global_dict
        self.line_dict = {}
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
        try:
            entry = self.line_dict[line]
        except KeyError:
            self.line_dict[line] = [code, 1, duration]
        else:
            entry[1] += 1
            entry[2] += duration

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
        key = (line, callee_file_timing.filename, callee)
        try:
            entry = self.call_dict[key]
        except KeyError:
            self.call_dict[key] = [code, 1, duration]
        else:
            entry[1] += 1
            entry[2] += duration

    def getHitStatsFor(self, line):
        code, line, duration = self.line_dict.get(line, (None, 0, 0))
        if code is None:
            firstlineno = None
        else:
            firstlineno = code.co_firstlineno
            code = code.co_name
        return code, firstlineno, line, duration

    def getCallListByLine(self):
        result = defaultdict(list)
        for (line, name, callee), (code, hit, duration) in \
                self.call_dict.iteritems():
            result[line].append((
                code.co_name, code.co_firstlineno,
                hit, duration,
                name, callee.co_firstlineno, callee.co_name,
            ))
        return result

    def getTotalTime(self):
        return sum(x[2] for x in self.line_dict.itervalues())

    def getTotalHitCount(self):
        return sum(x[1] for x in self.line_dict.itervalues())

    def getSortKey(self):
        # total duration first, then total hit count for statistical profiling
        result = [0, 0]
        for _, hit, duration in self.line_dict.itervalues():
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
    '%6s|%10s|' \
    '%13s|%13s|%7s|' \
    'Source code' % (
        'Line #', 'Hits',
        'Time', 'Time per hit', '%',
    )
_ANNOTATE_HORIZONTAL_LINE = ''.join(x == '|' and '+' or '-'
    for x in _ANNOTATE_HEADER)
_ANNOTATE_FORMAT = \
    '%(lineno)6i|%(hits)10i|' \
    '%(time)13g|%(time_per_hit)13g|%(percent)6.2f%%|' \
    '%(line)s'
_ANNOTATE_CALL_FORMAT = \
    '(call)|%(hits)10i|' \
    '%(time)13g|%(time_per_hit)13g|%(percent)6.2f%%|' \
    '# %(callee_file)s:%(callee_line)s %(callee_name)s'

def _initStack():
    return deque([[time(), None, None]])

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
    FileTiming = _FileTiming

    def __init__(self):
        self.file_dict = {}
        self.global_dict = {}
        self.total_time = 0

    def _getFileTiming(self, frame):
        try:
            return self.global_dict[id(frame.f_globals)]
        except KeyError:
            f_globals = frame.f_globals
            name = self._getFilename(frame.f_code.co_filename, f_globals)
            try:
                file_timing = self.file_dict[name]
            except KeyError:
                self.file_dict[name] = file_timing = self.FileTiming(
                    frame.f_code.co_filename,
                    name,
                    f_globals,
                    self,
                )
            self.global_dict[id(f_globals)] = file_timing
            return file_timing

    def _getFilename(self, filename, f_globals):
        """
        Overload in subclasses to customise filename generation.
        """
        return filename

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
        lineno = 0
        if call_list_by_line:
            last_call_line = max(call_list_by_line)
        else:
            last_call_line = 0
        file_timing = self.file_dict[name]
        while True:
            lineno += 1
            line = linecache.getline(file_timing.raw_filename, lineno,
                file_timing.global_dict)
            func, firstlineno, hits, duration = file_timing.getHitStatsFor(
                lineno)
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
                line = os.linesep
            yield lineno, func, firstlineno, hits, duration, line

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
        print('version: 1', file=out)
        if commandline is not None:
            print('cmd:', commandline, file=out)
        print('creator: pprofile', file=out)
        print('event: usphit :us/hit', file=out)
        print('events: hits us usphit', file=out)
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
        for name in self._getFileNameList(filename, may_sort=False):
            printable_name = convertPath(name)
            print('fl=%s' % printable_name, file=out)
            funcname = False
            call_list_by_line = file_dict[name].getCallListByLine()
            for lineno, func, firstlineno, hits, duration, _ in self._iterFile(
                    name, call_list_by_line):
                call_list = call_list_by_line.get(lineno, ())
                if not hits and not call_list:
                    continue
                if func is None:
                    func, firstlineno = call_list[0][:2]
                if funcname != func:
                    funcname = func
                    print('fn=%s' % _getFuncOrFile(func,
                        printable_name, firstlineno), file=out)
                ticks = int(duration * 1000000)
                if hits == 0:
                    ticksperhit = 0
                else:
                    ticksperhit = ticks / hits
                print(lineno, hits, ticks, int(ticksperhit), file=out)
                for _, _, hits, duration, callee_file, callee_line, \
                        callee_name in sorted(call_list, key=lambda x: x[2:4]):
                    callee_file = convertPath(callee_file)
                    print('cfl=%s' % callee_file, file=out)
                    print('cfn=%s' % _getFuncOrFile(callee_name,
                        callee_file, callee_line), file=out)
                    print('calls=%s' % hits, callee_line, file=out)
                    duration *= 1000000
                    print(lineno, hits, int(duration), int(duration / hits), file=out)

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
            print('Command line:', commandline, file=out)
        print('Total duration: %gs' % total_time, file=out)
        if not total_time:
            return
        def percent(value, scale):
            if scale == 0:
                return 0
            return value * 100 / float(scale)
        for name in self._getFileNameList(filename):
            file_timing = file_dict[name]
            file_total_time = file_timing.getTotalTime()
            call_list_by_line = file_timing.getCallListByLine()
            print('File:', name, file=out)
            print('File duration: %gs (%.2f%%)' % (file_total_time,
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
                    'lineno': lineno,
                    'hits': hits,
                    'time': duration,
                    'time_per_hit': time_per_hit,
                    'percent': percent(duration, total_time),
                    'line': line.rstrip(),
                }, file=out)
                for _, _, hits, duration, callee_file, callee_line, \
                        callee_name in call_list_by_line.get(lineno, ()):
                    print(_ANNOTATE_CALL_FORMAT % {
                        'hits': hits,
                        'time': duration,
                        'time_per_hit': duration / hits,
                        'percent': percent(duration, total_time),
                        'callee_file': callee_file,
                        'callee_line': callee_line,
                        'callee_name': callee_name,
                    }, file=out)

    def _iterRawFile(self, name):
        lineno = 0
        file_timing = self.file_dict[name]
        while True:
            lineno += 1
            line = linecache.getline(file_timing.raw_filename, lineno,
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
        with _open(filename, 'w', errors='replace') as out:
            self.annotate(out)

    def print_stats(self):
        """
        Similar to profile.Profile.print_stats .
        Returns None.
        """
        self.annotate(_reopen(sys.stdout, errors='replace'))

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
            dont_inherit=1):
        with fd:
            code = compile(fd.read(), fd_name, 'exec', flags=compile_flags,
                dont_inherit=dont_inherit)
        original_sys_argv = list(sys.argv)
        try:
            sys.argv[:] = argv
            return self.runctx(code, {
                '__file__': fd_name,
                '__name__': '__main__',
                '__package__': None,
            }, None)
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
    stack = LocalDescriptor(_initStack)
    enabled_start = LocalDescriptor(float)
    discount_stack = LocalDescriptor(partial(deque, [0]))

    def __init__(self, verbose=False):
        super(Profile, self).__init__()
        if verbose:
            self._global_trace = _verboseProfileDecorator(self)(
                self._global_trace)
            self._local_trace = _verboseProfileDecorator(self)(
                self._local_trace)

    def _enable(self):
        """
        Overload this method when subclassing. Called before actually
        enabling trace.
        """
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
        del self.enabled_start
        del self.stack
        del self.discount_stack

    def disable(self, threads=True):
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
        print('%10.6f%s%s %s:%s %s+%s %s' % (
            time() - self.enabled_start,
            ' ' * len(self.stack),
            event,
            f_code.co_filename,
            lineno,
            f_code.co_name,
            lineno - f_code.co_firstlineno,
            self.discount_stack[-1],
        ), file=sys.stderr)

    def _global_trace(self, frame, event, arg):
        local_trace = self._local_trace
        if local_trace is not None:
            now = time()
            self.stack.append([now, frame.f_lineno, now])
            self.discount_stack.append(0)
        return local_trace

    def _local_trace(self, frame, event, arg):
        if event == 'line' or event == 'return':
            event_time = time()
            stack = self.stack
            try:
                stack_entry = stack[-1]
            except IndexError:
                warn('Profiling stack underflow, disabling.')
                self.disable()
                return
            call_time, old_line, old_time = stack_entry
            try:
                duration = event_time - old_time
            except TypeError:
                pass
            else:
                discount_time = self.discount_stack[-1]
                if discount_time:
                    duration -= discount_time
                    self.discount_stack[-1] = 0
                self._getFileTiming(frame).hit(frame.f_code, old_line,
                    duration)
            if event == 'line':
                stack_entry[1] = frame.f_lineno
                stack_entry[2] = event_time
            else:
                stack.pop()
                self.discount_stack.pop()
                inclusive_duration = event_time - call_time
                self.discount_stack[-1] += inclusive_duration
                caller_frame = frame.f_back
                self._getFileTiming(caller_frame).call(
                    caller_frame.f_code, caller_frame.f_lineno,
                    self._getFileTiming(frame),
                    frame.f_code, inclusive_duration,
                    frame,
                )
        return self._local_trace

    # profile/cProfile-like API
    def run(self, cmd):
        """Similar to profile.Profile.run ."""
        import __main__
        dict = __main__.__dict__
        return self.runctx(cmd, dict, dict)

class ThreadProfile(Profile):
    """
    threading.Thread-aware version of Profile class.

    Threads started after enable() call will be profiled.
    After disable() call, threads will need to be switched into and trigger a
    trace event (typically a "line" event) before they can notice the
    disabling.
    """
    def __init__(self, **kw):
        super(ThreadProfile, self).__init__(**kw)
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
      profiler = StatisticProfile()
      pt = StatisticThread(profiler)
      with pt:
        # do stuff
      profiler.print_stats()
    """
    _test = None
    _start_time = None
    clean_exit = False

    def __init__(self, profiler, period=.001, single=True, group=None, name=None):
        """
        period (float)
          How many seconds to wait between consecutive samples.
          The smaller, the more profiling overhead, but the faster results
          become meaningful.
          The larger, the less profiling overhead, but requires long profiling
          session to get meaningful results.
          Available on instances as the "profiler" read-only property.
        single (bool)
          Profile only the thread which created this instance.
        group, name
          See Python's threading.Thread API.
        """
        if single:
            self._test = lambda x, ident=threading.current_thread().ident: ident == x
        super(StatisticThread, self).__init__(
            group=group,
            name=name,
        )
        self._stop_event = threading.Event()
        self._period = period
        self._profiler = profiler
        profiler.total_time = 0
        self.daemon = True

    @property
    def profiler(self):
        return self._profiler

    def start(self):
        self._start_time = time()
        self._can_run = True
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
            test = lambda x, ident=self.ident: ident != x
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

def main():
    format_dict = {
        'text': 'annotate',
        'callgrind': 'callgrind',
    }

    parser = argparse.ArgumentParser()
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
        'corresponding .py file as a script. When given, positional arguments'
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

    options = parser.parse_args()
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
        runner_method_args = (args[0], args)
        runner_method_id = 'runpath'
    else:
        args = [options.module]
        if options.script is not None:
            args.append(options.script)
        args.extend(options.argv)
        runner_method_args = (options.module, args)
        runner_method_id = 'runmodule'
    if options.format is None:
        if os.path.basename(options.out).startswith('cachegrind.out.'):
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
        getattr(runner, runner_method_id)(*runner_method_args)
    finally:
        if options.out == '-':
            out = _reopen(sys.stdout, errors='replace')
            close = lambda: None
        else:
            out = _open(options.out, 'w', errors='replace')
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
        getattr(prof, format_dict[options.format])(
            out,
            filename=filename_set,
            commandline=repr(args),
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

if __name__ == '__main__':
    main()
