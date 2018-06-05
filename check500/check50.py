#!/usr/bin/env python

from __future__ import print_function

import argparse
import errno
import hashlib
import imp
import inspect
import json
import os
import pexpect
import re
import requests
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import xml.etree.cElementTree as ET

from backports.shutil_which import which
from bs4 import BeautifulSoup
from contextlib import contextmanager
from functools import wraps
from pexpect.exceptions import EOF, TIMEOUT
from six.moves.urllib import parse as url
from termcolor import cprint

try:
    from shlex import quote
except ImportError:
    from pipes import quote

import config

@contextmanager
def cd(path):
    """can be used with a `with` statement to temporarily change directories"""
    cwd = os.getcwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(cwd)


def cleanup():
    """Remove temporary files at end of test."""
    if config.tempdir:
        shutil.rmtree(config.tempdir)


def copy(src, dst):
    """Copy src to dst, copying recursively if src is a directory"""
    try:
        shutil.copytree(src, os.path.join(dst, os.path.basename(src)))
    except (OSError, IOError) as e:
        if e.errno == errno.ENOTDIR:
            shutil.copy(src, dst)
        else:
            raise


def excepthook(cls, exc, tb):
    cleanup()

    # Class is a BaseException, better just quit.
    if not issubclass(cls, Exception):
        print()
        return

    if cls is InternalError:
        cprint(exc.msg, "red", file=sys.stderr)
    elif any(issubclass(cls, err) for err in [IOError, OSError]) and exc.errno == errno.ENOENT:
        cprint("{} not found".format(exc.filename), "red", file=sys.stderr)
    else:
        cprint("Sorry, something's wrong! Let sysadmins@cs50.harvard.edu know!", "red", file=sys.stderr)

    if config.args.verbose:
        traceback.print_exception(cls, exc, tb)


sys.excepthook = excepthook


def handler(number, frame):
    termcolor.cprint("Check cancelled.", "red")
    sys.exit(1)


def print_results(results, log=False):
    for result in results:
        if result["status"] == Checks.PASS:
            cprint(":) {}".format(result["description"]), "green")
        elif result["status"] == Checks.FAIL:
            cprint(":( {}".format(result["description"]), "red")
            if result["rationale"] is not None:
                cprint("    {}".format(result["rationale"]), "red")
            if result["helpers"] is not None:
                cprint("    {}".format(result["helpers"]), "red") # TODO remove
        elif result["status"] == Checks.SKIP:
            cprint(":| {}".format(result["description"]), "yellow")
            cprint("    {}".format(result.get("rationale") or "check skipped"), "yellow")

        if log:
            for line in result.get("log", []):
                print("    {}".format(line))


def print_json(results):
    output = []
    for result in results:
        obj = {
            "name": result["test"]._testMethodName,
            "status": result["status"],
            "data": result["test"].data,
            "description": result["description"],
            "helpers": result["helpers"],
            "log": result["test"].log,
            "rationale": str(result["rationale"]) if result["rationale"] else None
        }

        try:
            obj["mismatch"] = {
                "expected": result["rationale"].expected,
                "actual": result["rationale"].actual
            }
        except AttributeError:
            pass

        output.append(obj)
    print(json.dumps(output, cls=Encoder))


def import_checks(identifier):
    """
    Given an identifier of the form path/to/check@org/repo, clone
    the checks from github.com/org/repo (defaulting to cs50/checks
    if there is no @) into config.args.checkdir. Then extract child
    of Check class from path/to/check/check50/__init__.py and return it

    Throws ImportError on error
    """
    if not config.args.offline:
        try:
            slug, repo = identifier.split("@")
        except ValueError:
            slug, repo = identifier, "cs50/checks"

        try:
            org, repo = repo.split("/")
        except ValueError:
            raise InternalError(
                "expected repository to be of the form username/repository, but got \"{}\"".format(repo))

        checks_root = os.path.join(config.args.checkdir, org, repo)
        config.check_dir = os.path.join(checks_root, slug.replace("/", os.sep), "check50")

        if os.path.exists(checks_root):
            command = ["git", "-C", checks_root, "pull", "origin", "master"]
        else:
            command = ["git", "clone", "https://github.com/{}/{}".format(org, repo), checks_root]

        # Can't use subprocess.DEVNULL because it requires python 3.3.
        stdout = stderr = None if config.args.verbose else open(os.devnull, "wb")

        # Update checks via git.
        try:
            subprocess.check_call(command, stdout=stdout, stderr=stderr)
        except subprocess.CalledProcessError:
            raise InternalError("failed to clone checks")
    else:
        slug = os.path.join(config.args.checkdir, identifier)
        checks_root = slug
        config.check_dir = os.path.join(checks_root, slug.replace("/", os.sep), "check50")

    # Install any dependencies from requirements.txt either in the root of the
    # repository or in the directory of the specific check.
    for dir in [checks_root, os.path.dirname(config.check_dir)]:
        requirements = os.path.join(dir, "requirements.txt")
        if os.path.exists(requirements):
            args = ["install", "-r", requirements]
            # If we are not in a virtualenv, we need --user
            if not hasattr(sys, "real_prefix"):
                args.append("--user")

            if not config.args.verbose:
                args += ["--quiet"] * 3

            try:
                code = __import__("pip").main(args)
            except SystemExit as e:
                code = e.code

            if code:
                raise InternalError("failed to install dependencies in ({})".format(
                    requirements[len(config.args.checkdir) + 1:]))

    try:
        # Import module from file path directly.
        module = imp.load_source(slug, os.path.join(config.check_dir, "__init__.py"))
        # Ensure that there is exactly one class decending from Checks defined in this package.
        checks, = (cls for _, cls in inspect.getmembers(module, inspect.isclass)
                   if hasattr(cls, "_Checks__sentinel")
                   and cls.__module__.startswith(slug))
    except (OSError, IOError) as e:
        if e.errno != errno.ENOENT:
            raise
    except ValueError as e:
        pass
    else:
        return checks

    raise InternalError("invalid identifier")


def import_from(path):
    """helper function to make it easier for a check to import another check"""
    with cd(config.check_dir):
        abspath = os.path.abspath(os.path.join(path, "check50", "__init__.py"))
    return imp.load_source(os.path.basename(path), abspath)


class TestResult(unittest.TestResult):
    results = []

    def __init__(self):
        super(TestResult, self).__init__(self)

    def addSuccess(self, test):
        """Handle completion of test, regardless of outcome."""
        self.results.append({
            "description": test.shortDescription(),
            "helpers": test.helpers,
            "log": test.log,
            "rationale": test.rationale,
            "status": test.result,
            "test": test
        })

    def addError(self, test, err):
        test.log.append(str(err[1]))
        test.log += (line.rstrip() for line in traceback.format_tb(err[2]))
        test.log.append("Contact sysadmins@cs50.harvard.edu with the URL of this check!")
        self.results.append({
            "description": test.shortDescription(),
            "helpers": test.helpers,
            "log": test.log,
            "rationale": "check50 ran into an error while running checks!",
            "status": Checks.SKIP,
            "test": test
        })


def valgrind(func):
    if config.test_cases[-1] == func.__name__:
        frame = traceback.extract_stack(limit=2)[0]
        raise InternalError("invalid check in {} on line {} of {}:\n"
                            "@valgrind must be placed below @check"
                            .format(frame.name, frame.lineno, frame.filename))

    @wraps(func)
    def wrapper(self):
        if not which("valgrind"):
            raise Error("valgrind not installed", result=Checks.SKIP)

        self._valgrind = True
        try:
            func(self)
            self._check_valgrind()
        finally:
            self._valgrind = False
    return wrapper

def fail(child):
    return child.status.failed

# Decorator for checks
def check(dependency=None):
    def decorator(func):

        # add test to list of test, in order of declaration
        config.test_cases.append(func.__name__)

        @wraps(func)
        def wrapper(self):

            # Check if dependency failed.
            if dependency and config.test_results.get(dependency) != Checks.PASS:
                self.result = config.test_results[func.__name__] = Checks.SKIP
                self.rationale = "can't check until a frown turns upside down"
                return

            # Move files into this check's directory.
            self.dir = dst_dir = os.path.join(config.tempdir, self._testMethodName)
            src_dir = os.path.join(config.tempdir, dependency or "_")
            shutil.copytree(src_dir, dst_dir)

            os.chdir(self.dir)
            # Run the test, catch failures.
            try:
                func(self)
                if self.children and self.children[-1].status.failed: # TODO make less hacky
                    error = Error(self.children[-1].payload.error_message)
                    error.helpers = self.children[-1].payload.help_message
                    raise error
            except Error as e:
                self.rationale = e.rationale
                self.helpers = e.helpers
                result = e.result
            else:
                result = Checks.PASS

            self.result = config.test_results[func.__name__] = result

        return wrapper
    return decorator


class Encoder(json.JSONEncoder):
    """Custom class for JSON encoding."""

    def default(self, o):
        if o == EOF:
            return "EOF"
        return o.__dict__


class File(object):
    """Generic class to represent file in check directory."""

    def __init__(self, filename):
        self.filename = filename

    def read(self):
        with File._open(self.filename) as f:
            return f.read()

    @staticmethod
    def _open(file, mode="r"):
        if sys.version_info < (3, 0):
            return open(file, mode + "U")
        else:
            return open(file, mode, newline="\n")


class App(object):
    def __init__(self, test, path):
        dir, file = os.path.split(path)
        name, _ = os.path.splitext(file)

        # add directory of flask app to sys.path so we can import it properly
        prevpath = sys.path[0]
        try:
            sys.path[0] = os.path.abspath(dir or ".")
            mod = imp.load_source(name, file)
        except (OSError, IOError) as e:
            if e.errno == errno.ENOENT:
                e = Error("could not find {}".format(file))
            raise e
        finally:
            # restore sys.path
            sys.path[0] = prevpath

        try:
            app = mod.app
        except AttributeError:
            raise Error("{} does not contain an app".format(file))

        # initialize flask client
        app.testing = True
        self.client = app.test_client()

        self.test = test
        self.response = None

    def get(self, route, data=None, params=None, follow_redirects=True):
        """Send GET request to `route`."""
        return self._send("GET", route, data, params, follow_redirects=follow_redirects)

    def post(self, route, data=None, params=None, follow_redirects=True):
        """Send POST request to `route`."""
        return self._send("POST", route, data, params, follow_redirects=follow_redirects)

    def status(self, code=None):
        """Throw error if http status code doesn't equal `code`or return the status code if `code is None."""
        if code is None:
            return self.response.status_code

        self.test.log.append(
            "checking that status code {} is returned...".format(code))
        if code != self.response.status_code:
            raise Error("expected status code {}, but got {}".format(
                code, self.response.status_code))
        return self

    def raw_content(self, output=None, str_output=None):
        """Searches for `output` regex match within content of page, regardless of mimetype."""
        return self._search_page(output, str_output, self.response.data, lambda regex, content: regex.search(content.decode()))

    def content(self, output=None, str_output=None, **kwargs):
        """Searches for `output` regex within HTML page. kwargs are passed to BeautifulSoup's find function to filter for tags."""
        if self.response.mimetype != "text/html":
            raise Error("expected request to return HTML, but it returned {}".format(
                self.response.mimetype))

        return self._search_page(
            output,
            str_output,
            BeautifulSoup(self.response.data, "html.parser"),
            lambda regex, content, **kwargs: any(regex.search(str(tag)) for tag in content.find_all(**kwargs)))

    def _send(self, method, route, data, params, **kwargs):
        """Send request of type `method` to `route`"""
        route = self._fmt_route(route, params)
        self.test.log.append("sending {} request to {}".format(method.upper(), route))

        try:
            self.response = getattr(self.client, method.lower())(route, data=data, **kwargs)
        except BaseException as e:  # Catch all exceptions thrown by app
            # TODO: Change Finance starter code for edX and remove this as well as app.testing = True in __init__
            self.test.log.append("exception raised in application: {}: {}".format(type(e).__name__, e))
            raise Error("application raised an exception (see log for details)")

        return self

    def _search_page(self, output, str_output, content, match_fn, **kwargs):
        if output is None:
            return content

        if str_output is None:
            str_output = output

        self.test.log.append(
            "checking that \"{}\" is in page".format(str_output))
        regex = re.compile(output)

        if not match_fn(regex, content, **kwargs):
            raise Error("expected to find \"{}\" in page, but it wasn't found".format(str_output))

        return self

    @staticmethod
    def _fmt_route(route, params):
        parsed = url.urlparse(route)

        # convert params dict into urlencoded string
        params = url.urlencode(params) if params else ""

        # concatenate params
        param_str = "&".join((ps for ps in [params, parsed.query] if ps))
        if param_str:
            param_str = "?" + param_str

        # only display netloc if it isn't localhost
        return "".join([parsed.netloc if parsed.netloc != "localhost" else "", parsed.path, param_str])


class Status:
    def __init__(self):
        self.failed = False

class Payload:
    def __init__(self):
        self.output = ""
        self.exit = None
        self.error_message = ""
        self.help_message = ""

# Wrapper class for pexpect child
class Child(object):
    def __init__(self, test, child):
        self.test = test
        self.child = child
        self.payload = Payload()
        self.status = Status()

    def stdin(self, line, prompt=True, timeout=3):
        if self.status.failed:
            return self

        if line == EOF:
            self.test.log.append("sending EOF...")
        else:
            self.test.log.append("sending input {}...".format(line))

        if prompt:
            try:
                self.child.expect(".+", timeout=timeout)
            except (TIMEOUT, EOF):
                return self.fail("expected prompt for input, found none")

        if line == EOF:
            self.child.sendeof()
        else:
            self.child.sendline(line)
        return self

    def stdout(self, output=None, str_output=None, timeout=3):
        if self.status.failed:
            return self

        self.wait(timeout)

        if output:
            return self.match(output = output, str_output = str_output)

        return self

    def reject(self, timeout=1):
        if self.status.failed:
            return self

        self.test.log.append("checking that input was rejected...")
        try:
            self.wait(timeout)
        except Error as e:
            if not isinstance(e.__context__, TIMEOUT):
                raise
        else:
            return self.fail("expected program to reject input, but it did not")
        return self

    def exit(self, code=None, timeout=5):
        if self.status.failed:
            return self

        self.wait(timeout)

        if code is None:
            return self.payload.exit

        self.test.log.append("checking that program exited with status {}...".format(code))
        if self.payload.exit != code:
            return self.fail("expected exit code {}, not {}".format(code, self.status.exit))
        return self

    def wait(self, timeout=5):
        out = []
        end = time.time() + timeout
        while time.time() <= end:
            if not self.child.isalive():
                break
            try:
                bytes = self.child.read_nonblocking(size=1024, timeout=0)
            except TIMEOUT:
                pass
            except EOF:
                break
            except UnicodeDecodeError:
                return self.fail("output not valid ASCII text")
            else:
                out.append(bytes)
        else:
            e = Error("timed out while waiting for program to exit")
            e.__context__ = TIMEOUT(timeout)
            raise e


        # Read any remaining data in pipe.
        while True:
            try:
                bytes = self.child.read_nonblocking(size=1024, timeout=0)
            except (TIMEOUT, EOF):
                break
            else:
                out.append(bytes)

        self.payload.output = "".join(out).replace("\r\n", "\n").lstrip("\n")
        self.kill()

        if self.child.signalstatus == signal.SIGSEGV:
            return self.fail("failed to execute program due to segmentation fault")

        self.payload.exit = self.child.exitstatus
        return self

    def kill(self):
        self.child.close(force=True)
        return self

    def fail(self, error_message = ""):
        self.status.failed = True
        if not self.payload.error_message:
            self.payload.error_message = error_message
        return self

    def on(self, condition):
        new = Child(self.test, self.child)
        new.payload = self.payload
        if not condition(self):
            new.fail()
        return new

    def match(self, output, str_output = None):
        if self.status.failed:
            return self

        if str_output is None:
            str_output = output

        if output == EOF:
            self.test.log.append("checking for EOF...")
        else:
            self.test.log.append("checking for output \"{}\"...".format(str_output))

        # Files should be interpreted literally, anything else shouldn't be.
        try:
            output = output.read()
        except AttributeError:
            #expect = self.child.expect
            expect = lambda : bool(re.match(output, self.payload.output))
        else:
            #expect = self.child.expect_exact
            expect = lambda : output == self.payload.output

        if not expect():
            return self.fail(Mismatch(str_output, self.payload.output.replace("\r\n", "\n")))
        #except Exception:
        #    return self.fail("check50 could not verify output")

        # If we expected EOF and we still got output, report an error.
        if output == EOF and re.match(re.compile(".+" + EOF, re.DOTALL), self.payload.output[0]):
            return self.fail(Mismatch(EOF, self.child.before.replace("\r\n", "\n")))

        return self

    def help(self, message):
        if self.status.failed:
            return self

        self.payload.help_message = message
        return self

class Checks(unittest.TestCase):
    PASS = True
    FAIL = False
    SKIP = None

    _valgrind_log = "valgrind.xml"
    _valgrind = False

    # Here so we can properly check subclasses even when child is imported from another module.
    __sentinel = None

    def tearDown(self):
        while self.children:
            self.children.pop().kill()

    def __init__(self, method_name):
        super(Checks, self).__init__(method_name)
        self.result = self.FAIL
        self.rationale = None
        self.helpers = None
        self.log = []
        self.children = []
        self.data = {}

    def diff(self, f1, f2):
        """Returns boolean indicating whether or not the files are different"""
        if isinstance(f1, File):
            f1 = f1.filename
        if isinstance(f2, File):
            f2 = f2.filename
        return bool(self.spawn("diff {} {}".format(quote(f1), quote(f2)))
                        .wait()
                        .status.exit)

    def require(self, *paths):
        """Asserts that all paths exist."""
        for path in paths:
            self.log.append("Checking that {} exists...".format(path))
            if not os.path.exists(path):
                raise Error("{} not found".format(path))

    def hash(self, filename):
        """Hashes a file using SHA-256."""

        # Assert that file exists.
        if isinstance(filename, File):
            filename = filename.filename
        self.require(filename)

        # https://stackoverflow.com/a/22058673
        sha256 = hashlib.sha256()
        with open(filename, "rb") as f:
            while True:
                data = f.read(65536)
                if not data:
                    break
                sha256.update(data)
        return sha256.hexdigest()

    def spawn(self, cmd, env=None):
        """Spawns a new child process."""
        if self._valgrind:
            self.log.append("running valgrind {}...".format(cmd))
            cmd = "valgrind --show-leak-kinds=all --xml=yes --xml-file={} -- {}".format(
                os.path.join(self.dir, self._valgrind_log), cmd)
        else:
            self.log.append("running {}...".format(cmd))

        if env is None:
            env = {}
        env = os.environ.update(env)

        # Workaround for OSX pexpect bug http://pexpect.readthedocs.io/en/stable/commonissues.html#truncated-output-just-before-child-exits
        # Workaround from https://github.com/pexpect/pexpect/issues/373
        cmd = "bash -c {}".format(quote(cmd))
        if sys.version_info < (3, 0):
            child = pexpect.spawn(cmd, echo=False, env=env)
        else:
            child = pexpect.spawnu(cmd, encoding="utf-8", echo=False, env=env)

        self.children.append(Child(self, child))
        return self.children[-1]

    def flask(self, file):
        return App(self, file)

    def add(self, *paths):
        """Copies a file to the temporary directory."""
        cwd = os.getcwd()
        with cd(config.check_dir):
            for path in paths:
                copy(path, cwd)

    def append_code(self, original, codefile):
        if isinstance(original, File):
            original = original.filename

        if isinstance(codefile, File):
            codefile = codefile.filename

        with open(codefile) as code, open(original, "a") as o:
            o.write("\n")
            o.write(code.read())

    def replace_fn(self, old_fn, new_fn, filename):
        with open(filename, "r+") as f:
            asm = re.sub(r"(callq\t_?){}(?!\w)".format(old_fn), r"\1{}".format(new_fn), f.read())
            f.seek(0)
            f.write(asm)

    def _check_valgrind(self):
        """Log and report any errors encountered by valgrind"""
        # Load XML file created by valgrind
        xml = ET.ElementTree(file=os.path.join(self.dir, self._valgrind_log))

        self.log.append("checking for valgrind errors... ")

        # Ensure that we don't get duplicate error messages.
        reported = set()
        for error in xml.iterfind("error"):
            # Type of error valgrind encountered
            kind = error.find("kind").text

            # Valgrind's error message
            what = error.find("xwhat/text" if kind.startswith("Leak_") else "what").text

            # Error message that we will report
            msg = ["\t", what]

            # Find first stack frame within student's code.
            for frame in error.iterfind("stack/frame"):
                obj = frame.find("obj")
                if obj is not None and os.path.dirname(obj.text) == self.dir:
                    location = frame.find("file"), frame.find("line")
                    if None not in location:
                        msg.append(
                            ": (file: {}, line: {})".format(
                                location[0].text, location[1].text))
                    break

            msg = "".join(msg)
            if msg not in reported:
                self.log.append(msg)
                reported.add(msg)

        # Only raise exception if we encountered errors.
        if reported:
            raise Error("valgrind tests failed; rerun with --log for more information.")


class Mismatch(object):
    """Class which represents that expected output did not match actual output."""

    def __init__(self, expected, actual):
        self.expected = expected
        self.actual = actual

    def __str__(self):
        return "expected {}, not {}".format(self.raw(self.expected),
                                            self.raw(self.actual))

    def __repr__(self):
        return "Mismatch(expected={}, actual={})".format(repr(expected), repr(actual))

    @staticmethod
    def raw(s):
        """Get raw representation of s, truncating if too long"""

        if isinstance(s, list):
            s = "\n".join(s)

        if s == EOF:
            return "EOF"

        s = repr(s)  # get raw representation of string
        s = s[1:-1]  # strip away quotation marks
        if len(s) > 15:
            s = s[:15] + "..."  # truncate if too long
        return "\"{}\"".format(s)


class Error(Exception):
    """Class to wrap errors in students' checks."""

    def __init__(self, rationale=None, helpers=None, result=Checks.FAIL):
        self.rationale = rationale
        self.helpers = helpers
        self.result = result


class InternalError(Exception):
    """Error during execution of check50."""

    def __init__(self, msg):
        self.msg = msg


if __name__ == "__main__":
    main()
