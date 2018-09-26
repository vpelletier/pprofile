#!/usr/bin/env python
# Copyright (C) 2013-2018  Vincent Pelletier <plr.vincent@gmail.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
from os.path import join, dirname
import sys
from setuptools import setup

description = open(join(dirname(__file__), 'README.rst')).read()
setup(
    name='pprofile',
    version='2.0.0',
    author='Vincent Pelletier',
    author_email='plr.vincent@gmail.com',
    description=next(x for x in description.splitlines() if x.strip()),
    long_description='.. contents::\n\n' + description,
    url='http://github.com/vpelletier/pprofile',
    license='GPL 2+',
    platforms=['any'],
    classifiers=[
        'Intended Audience :: Developers',
        'License :: OSI Approved :: GNU General Public License v2 or later (GPLv2+)',
        'Operating System :: OS Independent',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: Implementation :: PyPy',
        'Programming Language :: Python :: Implementation :: CPython',
        'Programming Language :: Python :: Implementation :: IronPython',
        'Topic :: Software Development',
    ],
    py_modules=['pprofile', 'zpprofile'],
    entry_points={
        'console_scripts': [
            'pprofile=pprofile:main',
        ],
    },
    zip_safe=True,
    use_2to3=sys.version_info >= (3, ),
)
