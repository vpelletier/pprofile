Line-granularity, thread-aware deterministic pure-python profiler

Inspired from Robert Kern's line_profiler_ .

Overview
========

Python's standard profiling tools have a callable-level granularity, which
means it is only possible to tell which function is a hot-spot, not which
lines in that function.

Robert Kern's line_profiler_ is a very nice alternative providing line-level
profiling granularity, but in my opinion it has a few drawbacks which (in
addition to the attractive technical chalenge) made me start pprofile:

- It is not pure-python. This choice makes sense for performance
  but makes usage with pypy difficult and requires installation (I value
  execution straight from checkout).

- It requires source code modification to select what should be profiled.
  I prefer to have the option to do an in-depth, non-intrusive profiling.

- As an effect of previous point, it does not have a notion above individual
  callable, annotating functions but not whole files - preventing module
  import profiling.

- Profiling recursive code provides unexpected results (recursion cost is
  accumulated on callable's first line) because it doesn't track call stack.
  This may be unintended, and may be fixed at some point in line_profiler.

Usage
=====

As a command::

  $ pprofile some_python_executable

Once `some_python_executable` returns, prints annotated code of each file
involved in the execution (output can be directed to a file using `-o`/`--out`
arguments).

As a command with conflicting argument names: use "--" before profiled
executable name::

  $ pprofile -- foo --out bla

As a module::

  import pprofile

  profiler = pprofile.Profile()
  def someHotSpotCallable():
      with profiler:
          # Some hot-spot code

Alternative to `with`, allowing to end profiling in a different place::

  def someHotSpotCallable():
      profiler.enable()
      # Some hot-spot code
      someOtherFunction()

  def someOtherFunction():
      # Some more hot-spot code
      profiler.disable()

Then, to display anotated source on stdout::

  profiler.print_stats()

(several similar methods are available).

Sample output (standard threading.py removed from output for readability)::

  $ pprofile --threads 0 demo/threads.py
  Command line: ['demo/threads.py']
  Total duration: 1.00573s
  File: demo/threads.py
  File duration: 1.00168s (99.60%)
  Line #|      Hits|         Time| Time per hit|      %|Source code
  ------+----------+-------------+-------------+-------+-----------
       1|         2|  3.21865e-05|  1.60933e-05|  0.00%|import threading
       2|         1|  5.96046e-06|  5.96046e-06|  0.00%|import time
       3|         0|            0|            0|  0.00%|
       4|         2|   1.5974e-05|  7.98702e-06|  0.00%|def func():
       5|         1|      1.00111|      1.00111| 99.54%|  time.sleep(1)
       6|         0|            0|            0|  0.00%|
       7|         2|  2.00272e-05|  1.00136e-05|  0.00%|def func2():
       8|         1|  1.69277e-05|  1.69277e-05|  0.00%|  pass
       9|         0|            0|            0|  0.00%|
      10|         1|  1.81198e-05|  1.81198e-05|  0.00%|t1 = threading.Thread(target=func)
  (call)|         1|  0.000610828|  0.000610828|  0.06%|# /usr/lib/python2.7/threading.py:436 __init__
      11|         1|  1.52588e-05|  1.52588e-05|  0.00%|t2 = threading.Thread(target=func)
  (call)|         1|  0.000438929|  0.000438929|  0.04%|# /usr/lib/python2.7/threading.py:436 __init__
      12|         1|  4.79221e-05|  4.79221e-05|  0.00%|t1.start()
  (call)|         1|  0.000843048|  0.000843048|  0.08%|# /usr/lib/python2.7/threading.py:485 start
      13|         1|  6.48499e-05|  6.48499e-05|  0.01%|t2.start()
  (call)|         1|   0.00115609|   0.00115609|  0.11%|# /usr/lib/python2.7/threading.py:485 start
      14|         1|  0.000205994|  0.000205994|  0.02%|(func(), func2())
  (call)|         1|      1.00112|      1.00112| 99.54%|# demo/threads.py:4 func
  (call)|         1|  3.09944e-05|  3.09944e-05|  0.00%|# demo/threads.py:7 func2
      15|         1|  7.62939e-05|  7.62939e-05|  0.01%|t1.join()
  (call)|         1|  0.000423908|  0.000423908|  0.04%|# /usr/lib/python2.7/threading.py:653 join
      16|         1|  5.26905e-05|  5.26905e-05|  0.01%|t2.join()
  (call)|         1|  0.000320196|  0.000320196|  0.03%|# /usr/lib/python2.7/threading.py:653 join

Note that time.sleep call is not counted as such. For some reason, python is
not generating c_call/c_return/c_exception events (which are ignored by current
code, as a result).

Generating callgrind_-format output in a file instead of stdout::

  $ pprofile --format callgrind --out treads.log demo/threads.py

Can be opened, for example, with kcachegrind_.

Advanced
--------

*Warning*: API described here may change as I get a better understanding of what
is really needed (are filename + globals enough ? maybe the whole frame is
needed ?).

Both classes can be subclassed to customise file name generation. This is for
example useful when profiling Zope's Python Scripts. The following can be used
to allow profiling from restricted environment::

  import pprofile
  class ZopeProfiler(pprofile.Profile):
      __allow_access_to_unprotected_subobjects__ = 1
      def _getFilename(self, filename, f_globals):
          if 'Script (Python)' in filename and 'script' in f_globals:
              filename = f_globals['script'].id
          return filename

You will also want to monkey-patch linecache so that it becomes able to fetch
source code from Python Scripts::

  import linecache
  linecache_getlines = linecache.getlines
  def getlines(filename, module_globals=None):
      if module_globals is not None and \
              'Script (Python)' in filename and \
              'script' in module_globals:
          return module_globals['script'].body().splitlines()
      return linecache_getlines(filename, module_globals)
  linecache.getlines = getlines

Of course, allowing such access from Restricted Python has **security
implications**, depending on who has access to it. You decide and take
responsability.

Profiling such level of complex code as Zope (bonus points when profiling
template rendering) is not an easy task. Tweak proposed ZopeProfiler class
as you see fit for your profiling case - this is one of the reasons why no
such implementation is proposed ready-to-use (I don't see a one-size-fits-all
for this yet).

Thread-aware profiling
======================

ThreadProfile class provides the same features are Profile, but uses
`threading.settrace` to propagate tracing to `threading.Thread` threads started
after profiling is enabled.

Limitations
-----------

The time spent in another thread is not discounted from interrupted line.
On the long run, it should not be a problem if switches are evenly distributed
among lines, but threads executing fewer lines will appear as eating more cpu
time than they really do.

This is not specific to simultaneous multi-thread profiling: profiling a single
thread of a multi-threaded application will also be polluted by time spent in
other threads.

Example (lines are reported as taking longer to execute when profiled along
with another thread - although the other thread is not profiled)::

  $ demo/embedded.py
  Total duration: 1.00013s
  File: demo/embedded.py
  File duration: 1.00003s (99.99%)
  Line #|      Hits|         Time| Time per hit|      %|Source code
  ------+----------+-------------+-------------+-------+-----------
       1|         0|            0|            0|  0.00%|#!/usr/bin/env python
       2|         0|            0|            0|  0.00%|import threading
       3|         0|            0|            0|  0.00%|import pprofile
       4|         0|            0|            0|  0.00%|import time
       5|         0|            0|            0|  0.00%|import sys
       6|         0|            0|            0|  0.00%|
       7|         1|   1.5974e-05|   1.5974e-05|  0.00%|def func():
       8|         0|            0|            0|  0.00%|  # Busy loop, so context switches happe, so context switches happenn
       9|         1|  1.40667e-05|  1.40667e-05|  0.00%|  end = time.time() + 1
      10|    146604|     0.511392|  3.48826e-06| 51.13%|  while time.time() < end:
      11|    146603|      0.48861|  3.33288e-06| 48.85%|    pass
      12|         0|            0|            0|  0.00%|
      13|         0|            0|            0|  0.00%|# Single-treaded run
      14|         0|            0|            0|  0.00%|prof = pprofile.Profile()
      15|         0|            0|            0|  0.00%|with prof:
      16|         0|            0|            0|  0.00%|  func()
  (call)|         1|      1.00003|      1.00003| 99.99%|# ./demo/embedded.py:7 func
      17|         0|            0|            0|  0.00%|prof.annotate(sys.stdout, __file__)
      18|         0|            0|            0|  0.00%|
      19|         0|            0|            0|  0.00%|# Dual-threaded run
      20|         0|            0|            0|  0.00%|t1 = threading.Thread(target=func)
      21|         0|            0|            0|  0.00%|prof = pprofile.Profile()
      22|         0|            0|            0|  0.00%|with prof:
      23|         0|            0|            0|  0.00%|  t1.start()
      24|         0|            0|            0|  0.00%|  func()
      25|         0|            0|            0|  0.00%|  t1.join()
      26|         0|            0|            0|  0.00%|prof.annotate(sys.stdout, __file__)
  Total duration: 1.00129s
  File: demo/embedded.py
  File duration: 1.00004s (99.88%)
  Line #|      Hits|         Time| Time per hit|      %|Source code
  ------+----------+-------------+-------------+-------+-----------
  [...]
       7|         1|  1.50204e-05|  1.50204e-05|  0.00%|def func():
       8|         0|            0|            0|  0.00%|  # Busy loop, so context switches happe, so context switches happenn
       9|         1|  2.38419e-05|  2.38419e-05|  0.00%|  end = time.time() + 1
      10|     64598|     0.538571|  8.33728e-06| 53.79%|  while time.time() < end:
      11|     64597|     0.461432|  7.14324e-06| 46.08%|    pass
  [...]

This also means that the sum of the percentage of all lines can exceed 100%. It
can reach the number of concurrent threads (200% with 2 threads being busy for
the whole profiled executiong time, etc).

Example with 3 threads (same as first example, this time with thread profiling
enabled)::

  $ pprofile demo/threads.py
  Command line: ['demo/threads.py']
  Total duration: 1.00798s
  File: demo/threads.py
  File duration: 3.00604s (298.22%)
  Line #|      Hits|         Time| Time per hit|      %|Source code
  ------+----------+-------------+-------------+-------+-----------
       1|         2|  3.21865e-05|  1.60933e-05|  0.00%|import threading
       2|         1|  6.91414e-06|  6.91414e-06|  0.00%|import time
       3|         0|            0|            0|  0.00%|
       4|         4|  3.91006e-05|  9.77516e-06|  0.00%|def func():
       5|         3|      3.00539|       1.0018|298.16%|  time.sleep(1)
       6|         0|            0|            0|  0.00%|
       7|         2|  2.31266e-05|  1.15633e-05|  0.00%|def func2():
       8|         1|  2.38419e-05|  2.38419e-05|  0.00%|  pass
       9|         0|            0|            0|  0.00%|
      10|         1|  1.81198e-05|  1.81198e-05|  0.00%|t1 = threading.Thread(target=func)
  (call)|         1|  0.000612974|  0.000612974|  0.06%|# /usr/lib/python2.7/threading.py:436 __init__
      11|         1|  1.57356e-05|  1.57356e-05|  0.00%|t2 = threading.Thread(target=func)
  (call)|         1|  0.000438213|  0.000438213|  0.04%|# /usr/lib/python2.7/threading.py:436 __init__
      12|         1|  6.60419e-05|  6.60419e-05|  0.01%|t1.start()
  (call)|         1|  0.000913858|  0.000913858|  0.09%|# /usr/lib/python2.7/threading.py:485 start
      13|         1|   6.8903e-05|   6.8903e-05|  0.01%|t2.start()
  (call)|         1|   0.00167513|   0.00167513|  0.17%|# /usr/lib/python2.7/threading.py:485 start
      14|         1|  0.000200272|  0.000200272|  0.02%|(func(), func2())
  (call)|         1|      1.00274|      1.00274| 99.48%|# demo/threads.py:4 func
  (call)|         1|  4.19617e-05|  4.19617e-05|  0.00%|# demo/threads.py:7 func2
      15|         1|  9.58443e-05|  9.58443e-05|  0.01%|t1.join()
  (call)|         1|  0.000411987|  0.000411987|  0.04%|# /usr/lib/python2.7/threading.py:653 join
      16|         1|  5.29289e-05|  5.29289e-05|  0.01%|t2.join()
  (call)|         1|  0.000316143|  0.000316143|  0.03%|# /usr/lib/python2.7/threading.py:653 join

Note that the call time is not added to file total: it's already accounted
for inside "func".

.. _line_profiler: https://bitbucket.org/robertkern/line_profiler
.. _callgrind: http://valgrind.org/docs/manual/cl-format.html
.. _kcachegrind: http://kcachegrind.sourceforge.net
