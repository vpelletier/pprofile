#!/usr/bin/env python
from collections import defaultdict, deque
from functools import partial
from time import time
from warnings import warn
import argparse
import linecache
import os
import sys
import threading

class _FileTiming(object):
    __slots__ = ('line_dict', )
    def __init__(self):
        self.line_dict = defaultdict(lambda: [0, 0])

    def hit(self, line, duration):
        entry = self.line_dict[line]
        entry[0] += 1
        entry[1] += duration

    def getStatsFor(self, line):
        return self.line_dict.get(line, (0, 0))

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
        delattr(self, str(id(instance)))

_ANNOTATE_HEADER = '%6s|%10s|%13s|%13s|%7s|Source code' % (
    'Line #', 'Hits', 'Time', 'Time per hit', '%')
_ANNOTATE_HORIZONTAL_LINE = ''.join(x == '|' and '+' or '-'
    for x in _ANNOTATE_HEADER)
_ANNOTATE_FORMAT = '%(lineno)6i|%(hits)10i|%(time)13g|%(time_per_hit)13g|' \
    '%(percent)6.2f%%|%(line)s'

def _initStack():
    return deque(((time(), None, None), ))

class Profile(object):
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

    def __init__(self):
        self.file_dict = defaultdict(_FileTiming)
        self.total_time = 0

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

    __enter__ = enable

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disable()

    def _global_trace(self, frame, event, arg):
        local_trace = self._local_trace
        if local_trace is not None:
            self.stack.append((time(), None, None))
            self.discount_stack.append(0)
        return local_trace

    def _local_trace(self, frame, event, arg):
        if event == 'line' or event == 'return':
            # Access stack before getting event_time, as it may initialise the
            # stack with current time.
            stack = self.stack
            event_time = time()
            try:
                call_time, old_line, old_time = stack.pop()
            except IndexError:
                warn('Profiling stack underflow, disabling.')
                self.disable()
                return
            if old_line is not None:
                discount_time = self.discount_stack[-1]
                if discount_time:
                    self.discount_stack[-1] = 0
                    old_time += discount_time
                self.file_dict[frame.f_code.co_filename].hit(old_line,
                    event_time - old_time)
            if event == 'line':
                stack.append((call_time, frame.f_lineno, event_time))
            else:
                self.discount_stack.pop()
                self.discount_stack[-1] += event_time - call_time
        return self._local_trace

    def getFilenameSet(self):
        """
        Returns a set of profiled file names.

        Note: "file name" is used loosely here. See python documentation for
        co_filename, linecache module and PEP302. It may not be a valid
        filesystem path.
        """
        result = set(self.file_dict)
        # Ignore profiling code
        result.discard(__file__)
        return result

    def annotate(self, out, filename=None):
        """
        Dump annotated source code with current profiling statistics to "out"
        file.
        out (file-ish opened for writing)
            Destination of annotated sources.
        filename (str, list of str)
            If provided, dump stats for given source file(s) only.
            By default, list for all known files.
        """
        if filename is None:
            filename = sorted(self.getFilenameSet())
        elif isinstance(filename, basestring):
            filename = [filename]
        file_dict = self.file_dict
        if not file_dict:
            print >> out, '(no measure)'
        total_time = self.total_time
        print >> out, 'Total duration: %gs' % total_time
        for name in filename:
            file_timing = file_dict[name]
            print >> out, name
            print >> out, _ANNOTATE_HEADER
            print >> out, _ANNOTATE_HORIZONTAL_LINE
            lineno = 0
            while True:
                lineno += 1
                line = linecache.getline(name, lineno)
                hits, duration = file_timing.getStatsFor(lineno)
                if not line:
                    if hits == 0:
                        break
                    # Line exists in stats, but not in file. Happens on 1st
                    # line of empty files (ex: __init__.py). Fake the presence
                    # of an empty line.
                    line = '\n'
                if hits:
                    time_per_hit = duration / hits
                else:
                    time_per_hit = 0
                print >> out, _ANNOTATE_FORMAT % {
                  'lineno': lineno,
                  'hits': hits,
                  'time': duration,
                  'time_per_hit': time_per_hit,
                  'percent': duration * 100 / total_time,
                  'line': line,
                },

    # profile/cProfile-like API
    def dump_stats(self, filename):
        """Similar to profile.Profile.dump_stats ."""
        with open(filename, 'w') as out:
            self.annotate(out)

    def print_stats(self):
        """
        Similar to profile.Profile.print_stats .
        Returns None.
        """
        self.annotate(sys.stdout)

    def run(self, cmd):
        """Similar to profile.Profile.run ."""
        import __main__
        dict = __main__.__dict__
        return self.runctx(cmd, dict, dict)

    def runctx(self, cmd, globals, locals):
        """Similar to profile.Profile.runctx ."""
        with self:
            exec cmd in globals, locals
        return self

    def runcall(self, func, *args, **kw):
        """Similar to profile.Profile.runcall ."""
        with self:
            return func(*args, **kw)

class ThreadProfile(Profile):
    """
    threading.Thread-aware version of Profile class.

    Threads started after enable() call will be profiled.
    After disable() call, threads will need to be switched into and trigger a
    trace event (typically a "line" event) before they can notice the
    disabling.
    """
    def _enable(self):
        try:
            del self._local_trace
        except AttributeError:
            pass
        threading.settrace(self._global_trace)
        super(ThreadProfile, self)._enable()

    def _disable(self):
        super(ThreadProfile, self)._disable()
        threading.settrace(None)
        self._local_trace = None

# profile/cProfile-like API (no sort parameter !)
def _run(threads, func_name, filename, *args, **kw):
    if threads:
        klass = ThreadProfile
    else:
        klass = Profile
    prof = klass()
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

def run(cmd, filename=None, threads=True):
    """Similar to profile.run ."""
    _run(threads, 'run', filename, cmd)

def runctx(cmd, globals, locals, filename=None, threads=True):
    """Similar to profile.runctx ."""
    _run(threads, 'runctx', filename, cmd, globals, locals)

def runfile(fd, argv, filename=None, fd_name='<unknown>', threads=True):
    """
    Run code from given file descriptor with profiling enabled.
    Closes fd before executing contained code.
    """
    with fd:
        code = compile(fd.read(), fd_name, 'exec')
    original_sys_argv = list(sys.argv)
    try:
        sys.argv[:] = argv
        runctx(code, {
            '__file__': fd_name,
            '__name__': '__main__',
            '__package__': None,
        }, None, filename=filename, threads=threads)
    finally:
        sys.argv[:] = original_sys_argv

def runpath(path, argv, filename=None, threads=True):
    """
    Run code from open-accessible file path with profiling enabled.
    """
    sys.path.insert(0, os.path.dirname(path))
    runfile(open(path, 'rb'), argv, fd_name=path, filename=filename,
        threads=threads)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('script', help='Python script to execute (optionaly '
        'followed by its arguments)')
    parser.add_argument('-o', '--out', help='Write annotated sources to this '
        'file. Defaults to stdout.')
    parser.add_argument('-t', '--threads', default=1, type=int, help='If '
        'non-zero, trace threads spawned by program. Default: %(default)s')
    options, args = parser.parse_known_args()
    args.insert(0, options.script)
    runpath(options.script, args, filename=options.out,
        threads=bool(options.threads))

if __name__ == '__main__':
    main()
