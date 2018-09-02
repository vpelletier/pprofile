from time import sleep
MAX_LEVEL = 5
def bar(level=0):
  if level < MAX_LEVEL:
    bar(level + 1)
    bar(level + 1)
  sleep(0.01)
bar()
