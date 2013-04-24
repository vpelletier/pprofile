#!/usr/bin/env python

def trigger():
    raise Exception

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

# Uncaught exception, from function
try:
    trigger()
finally:
    pass

print 'Never reached'
