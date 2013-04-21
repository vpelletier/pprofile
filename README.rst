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

  pprofile some_python_executable

Once `some_python_executable` returns, prints annotated code of each file
involved in the execution (output can be directed to a file using `-o`/`--out`
arguments).

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

Sample output (pprofile.py and threading.py removed from output)::

  $ pprofile dummy.py
  0.0
  55
  9.26535896605e-05
  6765
  Total duration: 0.245515s
  dummy.py
  Line #|      Hits|         Time| Time per hit|      %|Source code
  ------+----------+-------------+-------------+-------+-----------
       1|         0|            0|            0|  0.00%|#!/usr/bin/env python
       2|         1|  7.15256e-06|  7.15256e-06|  0.00%|import threading
       3|         1|  0.000106812|  0.000106812|  0.04%|from dummy_module.fibo import fibo, sin
       4|         0|            0|            0|  0.00%|
       5|         1|  5.96046e-06|  5.96046e-06|  0.00%|def sin_printer(n):
       6|         2|    0.0359957|    0.0179979| 14.66%|    print sin(n)
       7|         0|            0|            0|  0.00%|
       8|         1|  4.05312e-06|  4.05312e-06|  0.00%|def main():
       9|         1|  1.21593e-05|  1.21593e-05|  0.00%|    t1 = threading.Thread(target=sin_printer, args=(0, ))
      10|         1|  1.19209e-05|  1.19209e-05|  0.00%|    t2 = threading.Thread(target=sin_printer, args=(3.1415, ))
      11|         1|  4.29153e-05|  4.29153e-05|  0.02%|    t1.start()
      12|         1|  0.000106812|  0.000106812|  0.04%|    print fibo(10)
      13|         1|  4.22001e-05|  4.22001e-05|  0.02%|    t2.start()
      14|         1|  5.10216e-05|  5.10216e-05|  0.02%|    print fibo(20)
      15|         1|  1.78814e-05|  1.78814e-05|  0.01%|    t1.join()
      16|         1|  1.19209e-05|  1.19209e-05|  0.00%|    t2.join()
      17|         0|            0|            0|  0.00%|
      18|         1|  5.00679e-06|  5.00679e-06|  0.00%|if __name__ == '__main__':
      19|         1|  1.38283e-05|  1.38283e-05|  0.01%|    main()
  dummy_module/__init__.py
  Line #|      Hits|         Time| Time per hit|      %|Source code
  ------+----------+-------------+-------------+-------+-----------
       1|         1|  2.14577e-06|  2.14577e-06|  0.00%|
  dummy_module/fibo.py
  Line #|      Hits|         Time| Time per hit|      %|Source code
  ------+----------+-------------+-------------+-------+-----------
       1|         1|  3.38554e-05|  3.38554e-05|  0.01%|import math
       2|         0|            0|            0|  0.00%|
       3|         1|  7.15256e-06|  7.15256e-06|  0.00%|def fibo(n):
       4|     13638|    0.0266435|  1.95362e-06| 10.85%|    assert n > 0, n
       5|     13638|    0.0526528|  3.86074e-06| 21.45%|    if n < 3:
       6|      6820|    0.0255547|  3.74702e-06| 10.41%|        return 1
       7|      6818|     0.108189|  1.58681e-05| 44.07%|    return fibo(n - 1) + fibo(n - 2)
       8|         0|            0|            0|  0.00%|
       9|         1|  5.00679e-06|  5.00679e-06|  0.00%|def sin(n):
      10|         2|  8.91685e-05|  4.45843e-05|  0.04%|    return math.sin(n)

Thread-aware profiling
======================

ThreadProfile class provides the same features are Profile, but propagates
tracing to `threading.Thread` threads started after profiling is enabled using
`threading.settrace`.

Limitations
-----------

The time spent in another thread is not discounted from interrupted line.
On the long run, it should not be a problem if switches are evenly distributed
among lines, but threads executing fewer lines will appear as eating more cpu
time than they really do.

This is not specific to simultaneous multi-thread profiling: profiling a single
thread of a multi-threaded application will also be polluted by time spent in
other threads.

This also means that the sum of the percentage of all lines can exceed 100%. It
can reach the number of concurrent threads (200% with 2 threads being busy for
the whole profiled executiong time, etc).

.. _line_profiler: https://bitbucket.org/robertkern/line_profiler
