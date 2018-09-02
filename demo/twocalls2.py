from time import sleep
def bar():
  sleep(0.1)
def foo():
  bar()
  bar()
foo()
