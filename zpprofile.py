"""
Zope-friendly layer for pprofile.

In Zope:
- Executed code is not necessarily a valid FS path (ex: Python Scripts)
- Executed code is not available to the machine where profiling results are
  analysed.
- Restricted Python cannot manipulate all desired types, and one may want to
  trigger profiling from its level.

This layer addresses all these issues, by making interesting pprofile classes
accessible to restricted python and bundling source code wxith profiling
results.

NOTE: This does allow anyone able to get profiler output to get whole source
files from your server. So better keep good track of who can profile and/or
where profiling results end. Alone, this module won't be accessible from
Restricted Python.

Example deterministic usage:
    # Get profiler (how you get to zpprofile module depends on your
    # application).
    profiler = zpprofile.getProfiler()
    # Get callable (to not profile how it is retrieved).
    func = context.somethingOrOther
    # Actually profile stuff
    with profiler:
        func()
    # Build response
    response = context.REQUEST.RESPONSE
    data, content_type = profiler.asZip()
    response.setHeader('content-type', content_type)
    response.setHeader(
        'content-disposition',
        'attachment; filename="' + func.id + '.zip"',
    )
    # Push response immediately (hopefully, profiled function did not write
    # anything on its own).
    response.write(data)
    # Make transaction fail, so any otherwise persistent change made by
    # profiled function is undone - note that many caches will still have
    # been warmed up, just as with any other code.
    raise Exception('profiling')

Example statistic usage (to profile other running threads):
    from time import sleep
    # Get profiler (how you get to zpprofile module depends on your
    # application).
    profiler, thread = zpprofile.getStatisticalProfilerAndThread(single=False)
    # Actually profile whatever is going on in the same process, just waiting.
    with thread:
        sleep(60)
    # Build response
    response = context.REQUEST.RESPONSE
    data, content_type = profiler.asZip()
    response.setHeader('content-type', content_type)
    response.setHeader(
        'content-disposition',
        'attachment; filename="statistical_' +
          DateTime().strftime('%Y%m%d%H%M%S') +
        '.zip"',
    )
    return data
"""
from __future__ import print_function
import dis
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.encoders import encode_quopri
import functools
import gc
from io import StringIO, BytesIO
from importlib import import_module
import itertools
import os
from collections import defaultdict
import zipfile
import pprofile

def getFuncCodeOrNone(module, attribute_path):
    try:
        value = import_module(module)
        for attribute in attribute_path:
            value = getattr(value, attribute)
        value = value.func_code
    except (ImportError, AttributeError):
        print('Could not reach func_code of module %r, attribute path %r' % (module, attribute_path))
        return None
    return value

DB_query_func_code = getFuncCodeOrNone('Products.ZMySQLDA.db', ('DB', '_query'))
ZODB_setstate_func_code = getFuncCodeOrNone('ZODB.Connection', ('Connection', '_setstate'))
PythonExpr__call__func_code = getFuncCodeOrNone('zope.tales.pythonexpr', ('PythonExpr', '__call__'))
ZRPythonExpr__call__func_code = getFuncCodeOrNone('Products.PageTemplates.ZRPythonExpr', ('PythonExpr', '__call__'))
DT_UtilEvaleval_func_code = getFuncCodeOrNone('DocumentTemplate.DT_Util', ('Eval', 'eval'))
SharedDCScriptsBindings_bindAndExec_func_code = getFuncCodeOrNone('Shared.DC.Scripts.Bindings', ('Bindings', '_bindAndExec'))

# OFS.Traversable.Traversable.unrestrictedTraverse overwites its path argument,
# preventing post-invocation introspection. As it does not mutate the argument,
# it is still possible to inspect using such controlled intermediate function.
def unrestrictedTraverse_spy(self, path, *args, **kw):
    return orig_unrestrictedTraverse(self, path, *args, **kw)
unrestrictedTraverse_spy_func_code = unrestrictedTraverse_spy.func_code
try:
    import OFS.Traversable
    orig_unrestrictedTraverse = OFS.Traversable.Traversable.unrestrictedTraverse
except (ImportError, AttributeError):
    pass
else:
    functools.update_wrapper(unrestrictedTraverse_spy, orig_unrestrictedTraverse)
    OFS.Traversable.Traversable.unrestrictedTraverse = unrestrictedTraverse_spy

_ALLSEP = os.sep + (os.altsep or '')
PYTHON_EXPR_FUNC_CODE_SET = (ZRPythonExpr__call__func_code, PythonExpr__call__func_code)

class ZopeFileTiming(pprofile.FileTiming):
    def call(self, code, line, callee_file_timing, callee, duration, frame):
        f_code = frame.f_code
        if f_code is DB_query_func_code:
            self.profiler.sql_dict[frame.f_locals['query']].append(duration)
        elif f_code is ZODB_setstate_func_code:
            f_locals = frame.f_locals
            obj = f_locals['obj']
            try:
                oid = obj._p_oid
            except AttributeError:
                pass
            else:
                self.profiler.zodb_dict[
                    f_locals['self'].db().database_name
                ][oid].append(duration)
        elif f_code is unrestrictedTraverse_spy_func_code:
            f_locals = frame.f_locals
            self.profiler.traverse_dict[
                (repr(f_locals['self']), repr(f_locals['path']))
            ].append(duration)
        super(ZopeFileTiming, self).call(
            code, line, callee_file_timing, callee, duration, frame,
        )

def tabulate(title_list, row_list):
    # de-lazify
    row_list = list(row_list)
    column_count = len(title_list)
    max_width_list = [len(x) for x in title_list]
    for row in row_list:
        assert len(row) == column_count, repr(row)
        for index, value in enumerate(row):
            max_width_list[index] = max(max_width_list[index], len(unicode(value)))
    format_string = u''.join(u'| %%-%is ' % x for x in max_width_list) + u'|\n'
    out = StringIO()
    write = out.write
    write(format_string % tuple(title_list))
    write(u''.join(u'+' + (u'-' * (x + 2)) for x in max_width_list) + u'+\n')
    for row in row_list:
        write(format_string % tuple(row))
    return out.getvalue()

def disassemble(co, lasti=-1):
    """Disassemble a code object."""
    # Taken from dis.disassemble, returns disassembled code instead of printing
    # it (the fuck python ?).
    # Also, unicodified.
    # Also, use % operator instead of string operations.
    # Also, one statement per line.
    out = StringIO()
    code = co.co_code
    labels = dis.findlabels(code)
    linestarts = dict(dis.findlinestarts(co))
    n = len(code)
    i = 0
    extended_arg = 0
    free = None
    while i < n:
        c = code[i]
        op = ord(c)
        if i in linestarts:
            if i > 0:
                print(end=u'\n', file=out)
            print(u'%3d' % linestarts[i], end=u' ', file=out)
        else:
            print(u'   ', end=u' ', file=out)

        if i == lasti:
            print(u'-->', end=u' ', file=out)
        else:
            print(u'   ', end=u' ', file=out)
        if i in labels:
            print(u'>>', end=u' ', file=out)
        else:
            print(u'  ', end=u' ', file=out)
        print(u'%4i' % i, end=u' ', file=out)
        print(u'%-20s' % dis.opname[op], end=u' ', file=out)
        i = i + 1
        if op >= dis.HAVE_ARGUMENT:
            oparg = ord(code[i]) + ord(code[i + 1]) * 256 + extended_arg
            extended_arg = 0
            i = i + 2
            if op == dis.EXTENDED_ARG:
                extended_arg = oparg * 65536L
            print(u'%5i' % oparg, end=u' ', file=out)
            if op in dis.hasconst:
                print(u'(%r)' % co.co_consts[oparg], end=u' ', file=out)
            elif op in dis.hasname:
                print(u'(%s)' % co.co_names[oparg], end=u' ', file=out)
            elif op in dis.hasjrel:
                print(u'(to %r)' % (i + oparg), end=u' ', file=out)
            elif op in dis.haslocal:
                print(u'(%s)' % co.co_varnames[oparg], end=u' ', file=out)
            elif op in dis.hascompare:
                print(u'(%s)' % dis.cmp_op[oparg], end=u' ', file=out)
            elif op in dis.hasfree:
                if free is None:
                    free = co.co_cellvars + co.co_freevars
                print(u'(%s)' % free[oparg], end=u' ', file=out)
        print(end=u'\n', file=out)
    return out.getvalue()

class ZopeMixIn(object):
    virtual__slots__ = (
        'sql_dict',
        'zodb_dict',
        'fake_source_dict',
        'traverse_dict',
    )
    __allow_access_to_unprotected_subobjects__ = 1
    FileTiming = ZopeFileTiming

    def __init__(self):
        super(ZopeMixIn, self).__init__()
        self.sql_dict = defaultdict(list)
        self.zodb_dict = defaultdict(lambda: defaultdict(list))
        self.fake_source_dict = {}
        self.traverse_dict = defaultdict(list)

    def _enable(self):
        gc.disable()
        super(ZopeMixIn, self)._enable()

    def _disable(self):
        super(ZopeMixIn, self)._disable()
        gc.enable()

    def _getline(self, filename, lineno, global_dict):
        line_list = self.fake_source_dict.get(filename)
        if line_list is None:
            return super(ZopeMixIn, self)._getline(
                filename,
                lineno,
                global_dict,
            )
        assert lineno > 0
        try:
            return line_list[lineno - 1]
        except IndexError:
            return ''

    def _rememberFile(self, source, suggested_name, extension):
        filename = suggested_name
        setdefault = self.fake_source_dict.setdefault
        suffix = itertools.count()
        source = source.splitlines(True)
        while setdefault(filename + extension, source) != source:
            filename = suggested_name + '_%i' % next(suffix)
        return filename + extension

    def _getFilename(self, frame):
        filename = super(ZopeMixIn, self)._getFilename(frame)
        if filename == 'Script (Python)':
            try:
                script = frame.f_globals['script']
            except KeyError:
                return filename
            return self._rememberFile(
                script.body().decode('utf-8') + (u'\n## %s\n' % script.id),
                script.id,
                '.py',
            )
        f_back = frame.f_back
        if f_back is not None:
            back_code = getattr(f_back, 'f_code')
            if filename == '<string>':
                if back_code is SharedDCScriptsBindings_bindAndExec_func_code:
                    return self._rememberFile(
                        u'# This is an auto-generated preamble executed by '
                        u'Shared.DC.Scripts.Bindings before "actual" code.\n' +
                        disassemble(frame.f_code),
                        'preamble',
                        '.py.bytecode',
                    )
                if back_code is DT_UtilEvaleval_func_code:
                    return self._rememberFile(
                        f_back.f_locals['self'].expr.decode('utf-8'),
                        'DT_Util_Eval',
                        '.py',
                    )
                return self._rememberFile(
                    u'# Unidentified source for <string>\n' + disassemble(
                        frame.f_code,
                    ),
                    '%s.%s' % (filename, frame.f_code.co_name),
                    '.py.bytecode',
                )
            if filename == 'PythonExpr':
                if back_code in PYTHON_EXPR_FUNC_CODE_SET:
                    return self._rememberFile(
                        f_back.f_locals['self'].text.decode('utf-8'),
                        'PythonExpr',
                        '.py',
                    )
                return self._rememberFile(
                    u'# Unidentified source for <PythonExpr>\n' + disassemble(
                        frame.f_code,
                    ),
                    '%s.%s' % (filename, frame.f_code.co_name),
                    '.py.bytecode',
                )
        return filename

    def _iterOutFiles(self):
        """
        Yields path, data, mimetype for each file involved on or produced by
        profiling.
        """
        out = StringIO()
        self.callgrind(out, relative_path=True)
        yield (
            'cachegrind.out.pprofile',
            out.getvalue(),
            'application/x-kcachegrind',
        )
        for name, lines in self.iterSource():
            lines = ''.join(lines)
            if lines:
                yield (
                    os.path.normpath(
                        os.path.splitdrive(name)[1]
                    ).lstrip(_ALLSEP),
                    lines,
                    'text/x-python',
                )
        sql_name_template = 'query_%%0%ii-%%i_hits_%%6fs.sql' % len(
            str(len(self.sql_dict)),
        )
        for index, (query, time_list) in enumerate(
            sorted(
                self.sql_dict.iteritems(),
                key=lambda x: (sum(x[1]), len(x[1])),
                reverse=True,
            ),
        ):
            yield (
                sql_name_template % (
                    index,
                    len(time_list),
                    sum(time_list),
                ),
                b'\n'.join(b'-- %10.6fs' % x for x in time_list) + b'\n' + query,
                'application/sql',
            )
        if self.zodb_dict:
            yield (
                'ZODB_setstate.txt',
                '\n\n'.join(
                    (
                        '%s (%fs)\n' % (
                            db_name,
                            sum(sum(x) for x in oid_dict.itervalues()),
                        )
                    ) + '\n'.join(
                        '%s (%i): %s' % (
                            oid.encode('hex'),
                            len(time_list),
                            ', '.join('%fs' % x for x in time_list),
                        )
                        for oid, time_list in oid_dict.iteritems()
                    )
                    for db_name, oid_dict in self.zodb_dict.iteritems()
                ),
                'text/plain',
            )
        if self.traverse_dict:
            yield (
                'unrestrictedTraverse_pathlist.txt',
                tabulate(
                    ('self', 'path', 'hit', 'total duration'),
                    sorted(
                        (
                            (context, path, len(duration_list), sum(duration_list))
                            for (context, path), duration_list in self.traverse_dict.iteritems()
                        ),
                        key=lambda x: x[3],
                        reverse=True,
                    ),
                ),
                'text/plain',
            )

    def asMIMEString(self):
        """
        Return a mime-multipart representation of:
        - callgrind profiling statistics (cachegrind.out.pprofile)
        - any SQL query issued via ZMySQLDA (query_*.sql)
        - any persistent object load via ZODB.Connection (ZODB_setstate.txt)
        - any path argument given to unrestrictedTraverse
          (unrestrictedTraverse_pathlist.txt)
        - all involved python code, including Python Scripts without hierarchy
          (the rest)
        To unpack resulting file, see "unpack a MIME message" in
          http://docs.python.org/2/library/email-examples.html
        Or get demultipart from
          https://pypi.python.org/pypi/demultipart
        """
        result = MIMEMultipart()
        base_type_dict = {
            'application': MIMEApplication,
            'text': MIMEText,
        }
        encoder_dict = {
            'application/x-kcachegrind': encode_quopri,
            'text/x-python': 'utf-8',
            'text/plain': 'utf-8',
        }
        for path, data, mimetype in self._iterOutFiles():
            base_type, sub_type = mimetype.split('/')
            chunk = base_type_dict[base_type](
                data,
                sub_type,
                encoder_dict.get(mimetype),
            )
            chunk.add_header(
                'Content-Disposition',
                'attachment',
                filename=path,
            )
            result.attach(chunk)
        return result.as_string(), result['content-type']

    def asZip(self):
        """
        Return a serialised zip archive containing:
        - callgrind profiling statistics (cachegrind.out.pprofile)
        - any SQL query issued via ZMySQLDA (query_*.sql)
        - any persistent object load via ZODB.Connection (ZODB_setstate.txt)
        - any path argument given to unrestrictedTraverse
          (unrestrictedTraverse_pathlist.txt)
        - all involved python code, including Python Scripts without hierarchy
          (the rest)
        """
        out = BytesIO()
        with zipfile.ZipFile(
            out,
            mode='w',
            compression=zipfile.ZIP_DEFLATED,
        ) as outfile:
            for path, data, _ in self._iterOutFiles():
                outfile.writestr(path, data)
        return out.getvalue(), 'application/zip'

class ZopeProfiler(ZopeMixIn, pprofile.Profile):
    __slots__ = ZopeMixIn.virtual__slots__

class ZopeStatisticalProfile(ZopeMixIn, pprofile.StatisticalProfile):
    __slots__ = ZopeMixIn.virtual__slots__

class ZopeStatisticalThread(pprofile.StatisticalThread):
    __allow_access_to_unprotected_subobjects__ = 1

# Intercept "verbose" parameter to prevent writing to stdout.
def getProfiler(verbose=False, **kw):
    """
    Get a Zope-friendly pprofile.Profile instance.
    """
    return ZopeProfiler(**kw)

def getStatisticalProfilerAndThread(**kw):
    """
    Get Zope-friendly pprofile.StatisticalProfile and
    pprofile.StatisticalThread instances.
    Arguments are forwarded to StatisticalThread.__init__ .
    """
    profiler = ZopeStatisticalProfile()
    return profiler, ZopeStatisticalThread(
        profiler=profiler,
        **kw
    )
