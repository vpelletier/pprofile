#!/usr/bin/env python
from collections import defaultdict, deque
from functools import partial, wraps
from time import time
from warnings import warn
import argparse
import linecache
import os
import sys
import threading
import thread

def _getFuncOrFile(func, module, line):
    if func == '<module>' or func is None:
        return module
    else:
        return '%s:%s' % (func, line)

class _FileTiming(object):
    __slots__ = ('line_dict', 'call_dict', 'filename', 'global_dict',
        'raw_filename')
    def __init__(self, raw_filename, filename, global_dict):
        self.raw_filename = raw_filename
        self.filename = filename
        self.global_dict = global_dict
        self.line_dict = {}
        self.call_dict = {}

    def hit(self, code, line, duration):
        try:
            entry = self.line_dict[line]
        except KeyError:
            self.line_dict[line] = [code, 1, duration]
        else:
            entry[1] += 1
            entry[2] += duration

    def call(self, code, line, callee_file_timing, callee, duration):
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
        try:
            delattr(self, str(id(instance)))
        except AttributeError:
            self.func

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
    def __init__(self):
        self.file_dict = {}
        self.global_dict = {}
        self.total_time = 0

    def _getFileTiming(self, frame):
        try:
            return self.global_dict[id(frame.f_globals)]
        except KeyError:
            f_globals = frame.f_globals
            raw_filename = frame.f_code.co_filename
            name = self._getFilename(frame.f_code.co_filename, f_globals)
            try:
                file_timing = self.file_dict[name]
            except KeyError:
                self.file_dict[name] = file_timing = _FileTiming(
                    raw_filename,
                    name,
                    f_globals,
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
        # Ignore profiling code
        result.discard(__file__)
        return result

    def _getFileNameList(self, filename):
        file_dict = self.file_dict
        if filename is None:
            return sorted(self.getFilenameSet(), reverse=True,
                key=lambda x: file_dict[x].getTotalTime())
        elif isinstance(filename, basestring):
            return [filename]
        return filename

    def _iterFile(self, name, call_list_by_line):
        lineno = 0
        last_call_line = max(call_list_by_line)
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

    def callgrind(self, out, filename=None, commandline=None):
        """
        Dump statistics in callgrind format.
        Contains:
        - per-line hit count, time and time-per-hit
        - call associations (call tree)
          Note: hit count is not inclusive, in that it is not the sum of all
          hits inside that call.
        Time unit: microsecond (1e-6 second).
        """
        print >> out, 'version: 1'
        if commandline is not None:
            print >> out, 'cmd:', commandline
        print >> out, 'creator: pprofile'
        print >> out, 'event: usphit :us/hit'
        print >> out, 'events: hits us usphit'
        file_dict = self.file_dict
        for name in self._getFileNameList(filename):
            print >> out, 'fl=%s' % name
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
                    print >> out, 'fn=%s' % _getFuncOrFile(func, name, firstlineno)
                ticks = int(duration * 1000000)
                if hits == 0:
                    ticksperhit = 0
                else:
                    ticksperhit = ticks / hits
                print >> out, lineno, hits, ticks, int(ticksperhit)
                for _, _, hits, duration, callee_file, callee_line, \
                        callee_name in sorted(call_list, key=lambda x: x[2:4]):
                    print >> out, 'cfl=%s' % callee_file
                    print >> out, 'cfn=%s' % _getFuncOrFile(callee_name,
                        callee_file, callee_line)
                    print >> out, 'calls=%s' % hits, callee_line
                    duration *= 1000000
                    print >> out, lineno, hits, int(duration), int(duration / hits)

    def annotate(self, out, filename=None, commandline=None):
        """
        Dump annotated source code with current profiling statistics to "out"
        file.
        out (file-ish opened for writing)
            Destination of annotated sources.
        filename (str, list of str)
            If provided, dump stats for given source file(s) only.
            By default, list for all known files.
        Time unit: second.
        """
        file_dict = self.file_dict
        total_time = self.total_time
        if commandline is not None:
            print >> out, 'Command line:', commandline
        print >> out, 'Total duration: %gs' % total_time
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
            print >> out, 'File:', name
            print >> out, 'File duration: %gs (%.2f%%)' % (file_total_time,
                percent(file_total_time, total_time))
            print >> out, _ANNOTATE_HEADER
            print >> out, _ANNOTATE_HORIZONTAL_LINE
            for lineno, _, _, hits, duration, line in self._iterFile(name,
                    call_list_by_line):
                if hits:
                    time_per_hit = duration / hits
                else:
                    time_per_hit = 0
                print >> out, _ANNOTATE_FORMAT % {
                    'lineno': lineno,
                    'hits': hits,
                    'time': duration,
                    'time_per_hit': time_per_hit,
                    'percent': percent(duration, total_time),
                    'line': line,
                },
                for _, _, hits, duration, callee_file, callee_line, \
                        callee_name in call_list_by_line.get(lineno, ()):
                    print >> out, _ANNOTATE_CALL_FORMAT % {
                        'hits': hits,
                        'time': duration,
                        'time_per_hit': duration / hits,
                        'percent': percent(duration, total_time),
                        'callee_file': callee_file,
                        'callee_line': callee_line,
                        'callee_name': callee_name,
                    }

    # profile/cProfile-like API
    def dump_stats(self, filename):
        """
        Similar to profile.Profile.dump_stats - but different output format !
        """
        with open(filename, 'w') as out:
            self.annotate(out)

    def print_stats(self):
        """
        Similar to profile.Profile.print_stats .
        Returns None.
        """
        self.annotate(sys.stdout)

class ProfileRunnerBase(object):
    # profile/cProfile-like API
    def runctx(self, cmd, globals, locals):
        """Similar to profile.Profile.runctx ."""
        with self:
            exec cmd in globals, locals
        return self

    def runcall(self, func, *args, **kw):
        """Similar to profile.Profile.runcall ."""
        with self:
            return func(*args, **kw)

    def runfile(self, fd, argv, fd_name='<unknown>', compile_flags=0,
            dont_inherit=0):
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

    __enter__ = enable

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disable()

    def _traceEvent(self, frame, event):
        f_code = frame.f_code
        lineno = frame.f_lineno
        print >> sys.stderr, '%10.6f%s%s %s:%s %s+%s %s' % (
            time() - self.enabled_start,
            ' ' * len(self.stack),
            event,
            f_code.co_filename,
            lineno,
            f_code.co_name,
            lineno - f_code.co_firstlineno,
            self.discount_stack[-1],
        )

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

class StatisticalProfile(ProfileBase):
    """
    Statistical profiling class.

    This class does not gather its own samples by itself.
    Instead, it must be provided with call stacks (as returned by
    sys._getframe() or sys._current_frames()).
    """
    def __init__(self):
        super(StatisticalProfile, self).__init__()
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
                called_timing, called_code, 0)
            called_timing = caller_timing
            frame = caller
            called_code = caller_code

class StatisticalThread(threading.Thread, ProfileRunnerBase):
    """
    Usage in a nutshell:
      profiler = StatisticalProfile()
      pt = StatisticalThread(profiler)
      with pt:
        # do stuff
      pt.stop()
      pt.join()
      profiler.print_stats()
    """
    _test = None
    _start_time = None

    def __init__(self, profiler, period=.001, single=True, group=None, name=None):
        """
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
        if single:
            self._test = lambda x, ident=thread.get_ident(): ident == x
        super(StatisticalThread, self).__init__(
            group=group,
            name=name,
        )
        self._stop_event = threading.Event()
        self._period = period
        self.profiler = profiler = StatisticalProfile()
        profiler.total_time = 0
        self.daemon = True

    def start(self):
        self._start_time = time()
        self._can_run = True
        super(StatisticalThread, self).start()

    def stop(self):
        """
        Request thread to stop.
        Does not wait for actual termination (use join() method).
        """
        if self.is_alive():
            self._can_run = False
            self._stop_event.set()
            self.profiler.total_time += time() - self._start_time
            self._start_time = None

    __enter__ = start

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        self.profiler.total_time += time() - self._start_time
        self._start_time = None
        self.join()

    def run(self):
        current_frames = sys._current_frames
        test = self._test
        if test is None:
            test = self.ident.__cmp__
        sample = self.profiler.sample
        stop_event = self._stop_event
        period = self._period
        wait = partial(stop_event.wait, period)
        while self._can_run:
            for ident, frame in current_frames().iteritems():
                if test(ident):
                    sample(frame)
            frame = None
            wait()
        stop_event.clear()

    def callgrind(self, *args, **kw):
        return self.profiler.callgrind(*args, **kw)

    def annotate(self, *args, **kw):
        return self.profiler.annotate(*args, **kw)

    def dump_stats(self, *args, **kw):
        return self.profiler.dump_stats(*args, **kw)

    def print_stats(self, *args, **kw):
        return self.profiler.print_stats(*args, **kw)

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

def runfile(fd, argv, fd_name='<unknown>', compile_flags=0, dont_inherit=0,
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

def main():
    format_dict = {
        'text': 'annotate',
        'callgrind': 'callgrind',
    }

    parser = argparse.ArgumentParser()
    parser.add_argument('script', help='Python script to execute (optionaly '
        'followed by its arguments)')
    parser.add_argument('-o', '--out', help='Write annotated sources to this '
        'file. Defaults to stdout.')
    parser.add_argument('-t', '--threads', default=1, type=int, help='If '
        'non-zero, trace threads spawned by program. Default: %(default)s')
    parser.add_argument('-f', '--format', default='text', choices=format_dict,
        help='Format in which output is generated. Default: %(default)s')
    parser.add_argument('-v', '--verbose', action='store_true',
        help='Enable profiler internal tracing output. Cryptic and verbose.')
    parser.add_argument('-s', '--statistic', default=0, type=float,
        help='Use this period for statistic profiling, or use deterministic '
        'profiling when 0.')
    options, args = parser.parse_known_args()
    args.insert(0, options.script)
    if options.statistic:
        prof = StatisticalThread(
            profiler=StatisticalProfile(),
            period=options.statistic,
            single=not options.threads,
        )
    else:
        if options.threads:
            klass = ThreadProfile
        else:
            klass = Profile
        prof = klass(verbose=options.verbose)
    try:
        prof.runpath(options.script, args)
    finally:
        if options.out is None:
            out = sys.stdout
            close = lambda: None
        else:
            out = open(options.out, 'w')
            close = out.close
        getattr(prof, format_dict[options.format])(
            out,
            commandline=repr(args),
        )
        close()

if __name__ == '__main__':
    main()
