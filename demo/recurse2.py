from time import sleep
MAX_LEVEL = 10
def boo(level=0):
  if level < MAX_LEVEL:
    baz(level + 1)
  sleep(0.01)
def baz(level):
  boo(level)
boo()
