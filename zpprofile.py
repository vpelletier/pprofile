"""
Zope-friendly layer for pprofile.

In Zope:
- Executed code is not necessarily a valid FS path (ex: Python Scripts)
- Executed code is not available to the machine where profiling results are
  analysed.
- Restricted Python cannot manipulate all desired types, and you may want to
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
    data, content_type = profiler.asMIMEString()
    response.setHeader('content-type', content_type)
    response.setHeader(
        'content-disposition',
        'attachment; filename="' + func.id + '.multipart"',
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
    data, content_type = profiler.asMIMEString()
    response.setHeader('content-type', content_type)
    response.setHeader(
        'content-disposition',
        'attachment; filename="statistical_' +
          DateTime().strftime('%Y%m%d%H%M%S') +
        '.multipart"',
    )
    return data
"""
from __future__ import print_function
import dis
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.encoders import encode_quopri
from io import StringIO
from importlib import import_module
import itertools
import os
from collections import defaultdict
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
        super(ZopeFileTiming, self).call(
            code, line, callee_file_timing, callee, duration, frame,
        )

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
    __allow_access_to_unprotected_subobjects__ = 1
    FileTiming = ZopeFileTiming

    def __init__(self):
        super(ZopeMixIn, self).__init__()
        self.sql_dict = defaultdict(list)
        self.zodb_dict = defaultdict(lambda: defaultdict(list))
        self.fake_source_dict = {}

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
                script.body() + '\n## %s\n' % script.id,
                script.id,
                '.py',
            )
        f_back = frame.f_back
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
                    f_back.f_locals['self'].expr,
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
        if filename == 'PythonExpr' and back_code in PYTHON_EXPR_FUNC_CODE_SET:
            return self._rememberFile(
                f_back.f_locals['self'].text,
                'PythonExpr',
                '.py',
            )
        return filename

    def asMIMEString(self):
        """
        Return a mime-multipart representation of:
        - callgrind profiling statistics (cachegrind.out.pprofile)
        - any SQL query issued via ZMySQLDA (query_*.sql)
        - any persistent object load via ZODB.Connection (ZODB_setstate.txt)
        - all involved python code, including Python Scripts without hierarchy
          (the rest)
        Does not rely on any local filesystem, which zipfile/tarfile would
        require.
        To unpack resulting file, see "unpack a MIME message" in
          http://docs.python.org/2/library/email-examples.html
        Or get demultipart from
          https://pypi.python.org/pypi/demultipart
        """
        result = MIMEMultipart()

        out = StringIO()
        self.callgrind(out, relative_path=True)
        profile = MIMEApplication(
            out.getvalue(),
            'x-kcachegrind',
            encode_quopri,
        )
        profile.add_header(
            'Content-Disposition',
            'attachment',
            filename='cachegrind.out.pprofile',
        )
        result.attach(profile)

        for name, lines in self.iterSource():
            lines = ''.join(lines)
            if lines:
                pyfile = MIMEText(lines, 'x-python', 'utf-8')
                pyfile.add_header(
                    'Content-Disposition',
                    'attachment',
                    filename=os.path.normpath(
                        os.path.splitdrive(name)[1]
                    ).lstrip(_ALLSEP),
                )
                result.attach(pyfile)

        sql_name_template = 'query_%%0%ii-%%i_hits_%%6fs.sql' % len(
            str(len(self.sql_dict))
        )
        for index, (query, time_list) in enumerate(
                    sorted(
                        self.sql_dict.iteritems(),
                        key=lambda x: (sum(x[1]), len(x[1])),
                        reverse=True,
                    ),
                ):
            sqlfile = MIMEApplication(
                '\n'.join(
                    '-- %10.6fs' % x
                    for x in time_list
                ) + '\n' + query, 'sql',
                encode_quopri,
            )
            sqlfile.add_header(
                'Content-Disposition',
                'attachment',
                filename=sql_name_template % (
                    index,
                    len(time_list),
                    sum(time_list),
                ),
            )
            result.attach(sqlfile)

        if self.zodb_dict:
            zodbfile = MIMEText('\n\n'.join(
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
            ), 'plain')
            zodbfile.add_header(
                'Content-Disposition',
                'attachment',
                filename='ZODB_setstate.txt',
            )
            result.attach(zodbfile)

        return result.as_string(), result['content-type']

class ZopeProfiler(ZopeMixIn, pprofile.Profile):
    pass

class ZopeStatisticalProfile(ZopeMixIn, pprofile.StatisticalProfile):
    pass

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
