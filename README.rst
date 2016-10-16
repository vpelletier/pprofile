Line-granularity, thread-aware deterministic and statistic pure-python profiler

Inspired from Robert Kern's line_profiler_ .

Usage
=====

As a command::

  $ pprofile some_python_executable arg1 ...

Once `some_python_executable` returns, prints annotated code of each file
involved in the execution.

As a command, ignoring any files from default `sys.path` (ie, python modules
themselves), for shorter output::

  $ pprofile --exclude-syspath some_python_executable arg1 ...

Executing a module, like :code:`python -m`. `--exclude-syspath` is not
recommended in this mode, as it will likely hide what you indent to profile.
Also, explicitly ending pprofile arguments with `--` will prevent accidentally
stealing command's arguments::

  $ pprofile -m some_python_module -- arg1 ...

As a module:

.. code:: python

  import pprofile

  def someHotSpotCallable():
      # Deterministic profiler
      prof = pprofile.Profile()
      with prof():
          # Code to profile
      prof.print_stats()

  def someOtherHotSpotCallable():
      # Statistic profiler
      prof = pprofile.StatisticalProfile()
      with prof(
          period=0.001, # Sample every 1ms
          single=True, # Only sample current thread
      ):
          # Code to profile
      prof.print_stats()

For advanced usage, see :code:`pprofile --help` and :code:`pydoc pprofile`.

Output
======

Supported output formats.

Callgrind
---------

The most useful output mode of pprofile is `Callgrind Profile Format`_, allows
browsing profiling results with kcachegrind_ (or qcachegrind_ on Windows).

::

  $ pprofile --format callgrind --out cachegrind.out.threads demo/threads.py

Callgrind format is implicitly enabled if ``--out`` basename starts with
``cachegrind.out.``, so above command can be simplified as::

  $ pprofile --out cachegrind.out.threads demo/threads.py

If you are analyzing callgrind traces on a different machine, you may want to
use the ``--zipfile`` option to generate a zip file containing all files::

  $ pprofile --out cachegrind.out.threads --zipfile threads_source.zip demo/threads.py

Generated files will use relative paths, so you can extract generated archive
in the same path as profiling result, and kcachegrind will load them - and not
your system-wide files, which may differ.

Annotated code
--------------

Human-readable output, but can become difficult to use with large programs.

::

  $ pprofile demo/threads.py

Profiling modes
===============

Deterministic profiling
-----------------------

In deterministic profiling mode, pprofile gets notified of each executed line.
This mode generates very detailed reports, but at the cost of a large overhead.
Also, profiling hooks being per-thread, either profiling must be enable before
spawning threads (if you want to profile more than just the current thread),
or profiled application must provide ways of enabling profiling afterwards
- which is not very convenient.

::

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

Statistic profiling
-------------------

In statistic profiling mode, pprofile periodically snapshots the current
callstack(s) of current process to see what is being executed.
As a result, profiler overhead can be dramatically reduced, making it possible
to profile real workloads. Also, as statistic profiling acts at the
whole-process level, it can be toggled independently of profiled code.

The downside of statistic profiling is that output lacks timing information,
which makes it harder to understand.

::

  $ pprofile --statistic .01 demo/threads.py
  Command line: ['demo/threads.py']
  Total duration: 1.0026s
  File: demo/threads.py
  File duration: 0s (0.00%)
  Line #|      Hits|         Time| Time per hit|      %|Source code
  ------+----------+-------------+-------------+-------+-----------
       1|         0|            0|            0|  0.00%|import threading
       2|         0|            0|            0|  0.00%|import time
       3|         0|            0|            0|  0.00%|
       4|         0|            0|            0|  0.00%|def func():
       5|       288|            0|            0|  0.00%|  time.sleep(1)
       6|         0|            0|            0|  0.00%|
       7|         0|            0|            0|  0.00%|def func2():
       8|         0|            0|            0|  0.00%|  pass
       9|         0|            0|            0|  0.00%|
      10|         0|            0|            0|  0.00%|t1 = threading.Thread(target=func)
      11|         0|            0|            0|  0.00%|t2 = threading.Thread(target=func)
      12|         0|            0|            0|  0.00%|t1.start()
      13|         0|            0|            0|  0.00%|t2.start()
      14|         0|            0|            0|  0.00%|(func(), func2())
  (call)|        96|            0|            0|  0.00%|# demo/threads.py:4 func
      15|         0|            0|            0|  0.00%|t1.join()
      16|         0|            0|            0|  0.00%|t2.join()
  File: /usr/lib/python2.7/threading.py
  File duration: 0s (0.00%)
  Line #|      Hits|         Time| Time per hit|      %|Source code
  ------+----------+-------------+-------------+-------+-----------
  [...]
     308|         0|            0|            0|  0.00%|    def wait(self, timeout=None):
  [...]
     338|         0|            0|            0|  0.00%|            if timeout is None:
     339|         1|            0|            0|  0.00%|                waiter.acquire()
     340|         0|            0|            0|  0.00%|                if __debug__:
  [...]
     600|         0|            0|            0|  0.00%|    def wait(self, timeout=None):
  [...]
     617|         0|            0|            0|  0.00%|            if not self.__flag:
     618|         0|            0|            0|  0.00%|                self.__cond.wait(timeout)
  (call)|         1|            0|            0|  0.00%|# /usr/lib/python2.7/threading.py:308 wait
  [...]
     724|         0|            0|            0|  0.00%|    def start(self):
  [...]
     748|         0|            0|            0|  0.00%|        self.__started.wait()
  (call)|         1|            0|            0|  0.00%|# /usr/lib/python2.7/threading.py:600 wait
     749|         0|            0|            0|  0.00%|
     750|         0|            0|            0|  0.00%|    def run(self):
  [...]
     760|         0|            0|            0|  0.00%|            if self.__target:
     761|         0|            0|            0|  0.00%|                self.__target(*self.__args, **self.__kwargs)
  (call)|       192|            0|            0|  0.00%|# demo/threads.py:4 func
     762|         0|            0|            0|  0.00%|        finally:
  [...]
     767|         0|            0|            0|  0.00%|    def __bootstrap(self):
  [...]
     780|         0|            0|            0|  0.00%|        try:
     781|         0|            0|            0|  0.00%|            self.__bootstrap_inner()
  (call)|       192|            0|            0|  0.00%|# /usr/lib/python2.7/threading.py:790 __bootstrap_inner
  [...]
     790|         0|            0|            0|  0.00%|    def __bootstrap_inner(self):
  [...]
     807|         0|            0|            0|  0.00%|            try:
     808|         0|            0|            0|  0.00%|                self.run()
  (call)|       192|            0|            0|  0.00%|# /usr/lib/python2.7/threading.py:750 run

Some details are lost (not all executed lines have a non-null hit-count), but
the hot spot is still easily identifiable in this trivial example, and its call
stack is still visible.

Thread-aware profiling
======================

``ThreadProfile`` class provides the same features as ``Profile``, but uses
``threading.settrace`` to propagate tracing to ``threading.Thread`` threads
started after profiling is enabled.

Limitations
-----------

The time spent in another thread is not discounted from interrupted line.
On the long run, it should not be a problem if switches are evenly distributed
among lines, but threads executing fewer lines will appear as eating more CPU
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
       8|         0|            0|            0|  0.00%|  # Busy loop, so context switches happen
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
       8|         0|            0|            0|  0.00%|  # Busy loop, so context switches happen
       9|         1|  2.38419e-05|  2.38419e-05|  0.00%|  end = time.time() + 1
      10|     64598|     0.538571|  8.33728e-06| 53.79%|  while time.time() < end:
      11|     64597|     0.461432|  7.14324e-06| 46.08%|    pass
  [...]

This also means that the sum of the percentage of all lines can exceed 100%. It
can reach the number of concurrent threads (200% with 2 threads being busy for
the whole profiled execution time, etc).

Example with 3 threads::

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

Why another profiler ?
======================

Python's standard profiling tools have a callable-level granularity, which
means it is only possible to tell which function is a hot-spot, not which
lines in that function.

Robert Kern's line_profiler_ is a very nice alternative providing line-level
profiling granularity, but in my opinion it has a few drawbacks which (in
addition to the attractive technical challenge) made me start pprofile:

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

.. _line_profiler: https://github.com/rkern/line_profiler
.. _`Callgrind Profile Format`: http://valgrind.org/docs/manual/cl-format.html
.. _kcachegrind: http://kcachegrind.sourceforge.net
.. _qcachegrind: http://sourceforge.net/projects/qcachegrindwin/
