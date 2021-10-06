#!/bin/sh
set -eu
repository="$(dirname "$0")"
cd "$repository"
for python in /usr/bin/python /usr/bin/python3 /usr/bin/pypy3; do
  echo "Testing with $python"
  testdir="$(mktemp --tmpdir --directory "pprofile_tests.XXXXXXXX")"
  trap 'rm -r "$testdir"' EXIT
  virtualenv -p "$python" "$testdir"
  "${testdir}/bin/pip" install "$repository"
  pprofile="${testdir}/bin/pprofile"

  "$pprofile" --include demo --threads 0 demo/threads.py
  "$pprofile" --include demo --format callgrind demo/threads.py
  "$pprofile" --include demo --statistic .01 demo/threads.py
  "${testdir}/bin/python" demo/embedded.py
  "$pprofile" --include demo demo/threads.py
  "$pprofile" --include demo demo/empty.py
  "$pprofile" --format callgrind demo/empty.py
  "$pprofile" --include demo --statistic .01 demo/empty.py
  "$pprofile" --format callgrind --zipfile "${testdir}/source_code.zip" demo/threads.py
  "$pprofile" --format callgrind --zipfile "${testdir}/source_code.zip" demo/empty.py
  "$pprofile" --exclude-syspath demo/threads.py
  "$pprofile" --exclude-syspath --statistic .01 demo/threads.py
  "$pprofile" --include demo demo/encoding.py
  LC_CTYPE=ISO-8859-15 "$pprofile" --include demo demo/encoding.py
  "$pprofile" --include demo demo/encoding.py > /dev/null
  "$pprofile" --include demo demo/empty.py -search
  "$pprofile" --include demo -- demo/empty.py -search
  "$pprofile" --include demo demo/recurse.py
  "$pprofile" --include demo demo/recurse2.py
  "$pprofile" --include demo demo/recurse3.py
  "$pprofile" --include demo demo/recurse4.py
  "$pprofile" --include demo demo/twocalls.py
  "$pprofile" --include demo demo/twocalls2.py
  "$pprofile" --include demo demo/the_main.py
  "$pprofile" --include demo demo/module_globals.py
  "$pprofile" --format callgrindzip --out "${testdir}/test_threads.zip" demo/threads.py && unzip -l "${testdir}/test_threads.zip"
  "$pprofile" --out "${testdir}/test_threads.zip" demo/threads.py && unzip -l "${testdir}/test_threads.zip"

  trap - EXIT
  rm -r "$testdir"
done
echo 'Success'
