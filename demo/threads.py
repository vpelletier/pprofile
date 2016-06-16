#!/usr/bin/env python
import threading
import time

def func():
  time.sleep(1)

def func2():
  pass

t1 = threading.Thread(target=func)
t2 = threading.Thread(target=func)
t1.start()
t2.start()
(func(), func2())
t1.join()
t2.join()
