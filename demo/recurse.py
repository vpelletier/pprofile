from time import sleep
MAX_LEVEL = 10
def foo(level=0):
  if level < MAX_LEVEL:
    foo(level + 1)
  sleep(0.01)
foo()
