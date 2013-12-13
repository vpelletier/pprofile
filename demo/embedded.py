#!/usr/bin/env python
import threading
import pprofile
import time
import sys

def func():
  # Busy loop, so context switches happen
  end = time.time() + 1
  while time.time() < end:
    pass

# Single-treaded run
prof = pprofile.Profile()
with prof:
  func()
prof.annotate(sys.stdout, __file__)

# Dual-threaded run
t1 = threading.Thread(target=func)
prof = pprofile.Profile()
with prof:
  t1.start()
  func()
  t1.join()
prof.annotate(sys.stdout, __file__)
