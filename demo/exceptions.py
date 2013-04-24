#!/usr/bin/env python

def trigger():
    raise Exception

def indirect():
    trigger()

# Caught exception
try:
    raise Exception
except Exception:
    pass

# Caught exception, from function
try:
    trigger()
except Exception:
    pass

# Caught exception, from deeper function
try:
    indirect()
except Exception:
    pass

# Uncaught exception, from function
try:
    trigger()
finally:
    pass

print 'Never reached'
