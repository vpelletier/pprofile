#!/usr/bin/env python
from time import time
import pprofile

def F():
    a, b = 0,1
    while True:
        yield a
        a, b = b, a + b

def benchmark():
    start = time()
    stop = start + 1
    for index, _ in enumerate(F()):
        now = time()
        if now > stop:
            break
    return index / (now - start)

raw = benchmark()
with pprofile.Profile():
    single = benchmark()
with pprofile.ThreadProfile():
    threaded = benchmark()
with pprofile.StatisticThread():
    statistic = benchmark()

for caption, value in (
    ('single', single),
    ('threaded', threaded),
    ('statistic', statistic),
):
    print('%s speed: %i%%' % (caption, value * 100 / raw))
