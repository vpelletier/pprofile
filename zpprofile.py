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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.encoders import encode_quopri
from cStringIO import StringIO
import os
from collections import defaultdict
import pprofile

_marker = object()

try:
    import Products.ZMySQLDA.db
    DB_query_func_code = Products.ZMySQLDA.db.DB._query.func_code
except (ImportError, AttributeError):
    DB_query_func_code = _marker

try:
    import ZODB.Connection
    ZODB_setstate_func_code = ZODB.Connection.Connection._setstate.func_code
except (ImportError, AttributeError):
    ZODB_setstate_func_code = _marker

_ALLSEP = os.sep + (os.altsep or '')

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

class ZopeMixIn(object):
    __allow_access_to_unprotected_subobjects__ = 1
    FileTiming = ZopeFileTiming

    def __init__(self):
        super(ZopeMixIn, self).__init__()
        self.sql_dict = defaultdict(list)
        self.zodb_dict = defaultdict(lambda: defaultdict(list))

    def _getFilename(self, filename, f_globals):
        if 'Script (Python)' in filename:
            try:
                script = f_globals['script']
            except KeyError:
                pass
            else:
                filename = script.id
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
                pyfile = MIMEText(lines, 'x-python')
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
