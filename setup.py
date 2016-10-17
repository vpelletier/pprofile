from os.path import join, dirname
import sys
from setuptools import setup
extra = {}
if sys.version_info >= (3, ):
    extra['use_2to3'] = True

description = open(join(dirname(__file__), 'README.rst')).read()

setup(
    name='pprofile',
    version='1.10.0',
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
    **extra
)
