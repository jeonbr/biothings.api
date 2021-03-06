# from http://asyncssh.readthedocs.io/en/latest/#id13

# To run this program, the file ``ssh_host_key`` must exist with an SSH
# private key in it to use as a server host key.

import os, glob, re, pickle, datetime, json, pydoc
import asyncio, asyncssh, crypt, sys, io
import types, aiocron, time
from functools import partial
from IPython import InteractiveShell
import psutil
from pprint import pprint
from collections import OrderedDict

from biothings import config
logging = config.logger
from biothings.utils.common import timesofar, sizeof_fmt
import biothings.utils.aws as aws

# useful variables to bring into hub namespace
pending = "pending"
done = "done"

HUB_ENV = hasattr(config,"HUB_ENV") and config.HUB_ENV or "" # default: prod (or "normal")
VERSIONS = HUB_ENV and "%s-versions" % HUB_ENV or "versions"
LATEST = HUB_ENV and "%s-latest" % HUB_ENV or "latest"


##############
# HUB SERVER #
##############

class HubSSHServerSession(asyncssh.SSHServerSession):

    running_jobs = {}
    job_cnt = 1

    def __init__(self, name, commands, extra_ns):
        # update with ssh server default commands
        self.commands = commands
        self.extra_ns = extra_ns
        self.extra_ns["cancel"] = self.__class__.cancel
        # for boolean calls
        self.extra_ns["_and"] = _and
        self.extra_ns["partial"] = partial
        self.extra_ns["hub"] = self
        self.commands["help"] = self.help
        # merge official/public commands with hidden/private to
        # make the whole available in shell's namespace
        self.extra_ns.update(self.commands)
        self.shell = InteractiveShell(user_ns=self.extra_ns)
        self.name = name
        self._input = ''

    def help(self, func=None):
        """
        Display help on given function/object or list all available commands
        """
        if not func:
            cmds = "\nAvailable commands:\n\n"
            for k in self.commands:
                cmds += "\t%s\n" % k
            cmds += "\nType: 'help(command)' for more\n"
            return cmds
        elif isinstance(func,partial):
            docstr = "\n" + pydoc.render_doc(func.func,title="Hub documentation: %s")
            docstr += "\nDefined et as a partial, with:\nargs:%s\nkwargs:%s\n" % (repr(func.args),repr(func.keywords))
            return docstr
        elif isinstance(func,HubCommand):
            docstr = "\nComposite command:\n\n%s\n" % func
            return docstr
        else:
            try:
                return "\n" + pydoc.render_doc(func,title="Hub documentation: %s")
            except ImportError:
                return "\nHelp not available for this command\n"

    def connection_made(self, chan):
        self._chan = chan
        self.origout = sys.stdout
        self.buf = io.StringIO()
        sys.stdout = self.buf

    def shell_requested(self):
        return True

    def exec_requested(self,command):
        self.data_received("%s\n" % command,None)
        return True

    def session_started(self):
        self._chan.write('\nWelcome to %s, %s!\n' % (self.name,self._chan.get_extra_info('username')))
        self._chan.write('hub> ')

    def data_received(self, data, datatype):
        self._input += data

        lines = self._input.split('\n')
        for line in lines[:-1]:
            if not line:
                continue
            line = line.strip()
            if line in [j["cmd"] for j in self.__class__.running_jobs.values()]:
                self._chan.write("Command '%s' is already running\n" % repr(line))
                continue
            self.origout.write("line %s\n" % line)
            # is it a hub command, in which case, intercept and run the actual declared cmd
            m = re.match("(.*)\(.*\)",line)
            if m:
                cmd = m.groups()[0].strip()
                if cmd in self.commands and \
                        isinstance(self.commands[cmd],HubCommand):
                    self.origout.write("%s -> %s\n" % (line,self.commands[cmd]))
                    line = self.commands[cmd]
            # cmdline is the actual command sent to shell, line is the one displayed
            # they can be different if there's a preprocessing
            cmdline = line
            # && jobs ? ie. chained jobs
            if "&&" in line:
                chained_jobs = [cmd for cmd in map(str.strip,line.split("&&")) if cmd]
                if len(chained_jobs) > 1:
                    # need to build a command with _and and using partial, meaning passing original func param
                    # to the partials
                    strjobs = []
                    for job in chained_jobs:
                        func,args = re.match("(.*)\((.*)\)",job).groups()
                        if args:
                            strjobs.append("partial(%s,%s)" % (func,args))
                        else:
                            strjobs.append("partial(%s)" % func)
                    cmdline = "_and(%s)" % ",".join(strjobs)
                else:
                    self._chan.write("Error: using '&&' operator required two operands\n")
                    continue
            logging.info("Run: %s " % repr(cmdline))
            r = self.shell.run_cell(cmdline,store_history=True)
            if not r.success:
                self.origout.write("Error\n")
                self._chan.write("Error: %s\n" % repr(r.error_in_exec))
            else:
                if r.result is None:
                    self.origout.write("OK: %s\n" % repr(r.result))
                    self.buf.seek(0)
                    self._chan.write(self.buf.read())
                    # clear buffer
                    self.buf.seek(0)
                    self.buf.truncate()
                else:
                    if type(r.result) == asyncio.tasks.Task or type(r.result) == asyncio.tasks._GatheringFuture or \
                            type(r.result) == asyncio.Future or \
                            type(r.result) == list and len(r.result) > 0 and type(r.result[0]) == asyncio.tasks.Task:
                        r.result = type(r.result) != list and [r.result] or r.result
                        self.__class__.running_jobs[self.__class__.job_cnt] = {"started_at" : time.time(),
                                                           "jobs" : r.result,
                                                           "cmd" : line}
                        self.__class__.job_cnt += 1
                    else:
                        self._chan.write(str(r.result) + '\n')
        if self.__class__.running_jobs:
            finished = []
            for num,info in sorted(self.__class__.running_jobs.items()):
                is_done = set([j.done() for j in info["jobs"]]) == set([True])
                has_err = is_done and  [True for j in info["jobs"] if j.exception()] or None
                outputs = is_done and ([str(j.exception()) for j in info["jobs"] if j.exception()] or \
                            [j.result() for j in info["jobs"]]) or None
                if is_done:
                    finished.append(num)
                    self._chan.write("[%s] %s %s: finished, %s\n" % (num,has_err and "ERR" or "OK ",info["cmd"], outputs))
                else:
                    self._chan.write("[%s] RUN {%s} %s\n" % (num,timesofar(info["started_at"]),info["cmd"]))
            if finished:
                for num in finished:
                    self.__class__.running_jobs.pop(num)

        self._chan.write('hub> ')
        self._input = lines[-1]

    def eof_received(self):
        self._chan.write('Have a good one...\n')
        self._chan.exit(0)

    def break_received(self, msec):
        # simulate CR
        self._chan.write('\n')
        self.data_received("\n",None)

    @classmethod
    def cancel(klass,jobnum):
        return klass.running_jobs.get(jobnum)


class HubSSHServer(asyncssh.SSHServer):

    COMMANDS = OrderedDict() # public hub commands
    EXTRA_NS = {} # extra commands, kind-of of hidden/private
    PASSWORDS = {}

    def session_requested(self):
        return HubSSHServerSession(self.__class__.NAME,
                                   self.__class__.COMMANDS,
                                   self.__class__.EXTRA_NS)

    def connection_made(self, conn):
        print('SSH connection received from %s.' %
                  conn.get_extra_info('peername')[0])

    def connection_lost(self, exc):
        if exc:
            print('SSH connection error: ' + str(exc), file=sys.stderr)
        else:
            print('SSH connection closed.')

    def begin_auth(self, username):
        # If the user's password is the empty string, no auth is required
        return self.__class__.PASSWORDS.get(username) != ''

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        pw = self.__class__.PASSWORDS.get(username, '*')
        return crypt.crypt(password, pw) == pw


@asyncio.coroutine
def start_server(loop,name,passwords,keys=['bin/ssh_host_key'],
                        host='',port=8022,commands={},extra_ns={}):
    for key in keys:
        assert os.path.exists(key),"Missing key '%s' (use: 'ssh-keygen -f %s' to generate it" % (key,key)
    HubSSHServer.PASSWORDS = passwords
    HubSSHServer.NAME = name
    if commands:
        HubSSHServer.COMMANDS.update(commands)
    if extra_ns:
        HubSSHServer.EXTRA_NS.update(extra_ns)
    yield from asyncssh.create_server(HubSSHServer, host, port, loop=loop,
                                 server_host_keys=keys)


####################
# DEFAULT HUB CMDS #
####################
# these can be used in client code to define
# commands. partial should be used to pass the
# required arguments, eg.:
# {"schedule" ; partial(schedule,loop)}

class JobRenderer(object):

    def __init__(self):
        self.rendered = {
                types.FunctionType : self.render_func,
                types.MethodType : self.render_method,
                partial : self.render_partial,
                types.LambdaType: self.render_lambda,
        }

    def render(self,job):
        r = self.rendered.get(type(job._callback))
        rstr = r(job._callback)
        delta = job._when - job._loop.time()
        strdelta = time.strftime("%Hh:%Mm:%Ss", time.gmtime(int(delta)))
        return "%s {run in %s}" % (rstr,strdelta)

    def render_partial(self,p):
        # class.method(args)
        return self.rendered[type(p.func)](p.func) + "%s" % str(p.args)

    def render_cron(self,c):
        # func type associated to cron can vary
        return self.rendered[type(c.func)](c.func) + " [%s]" % c.spec

    def render_func(self,f):
        return f.__name__

    def render_method(self,m):
        # what is self ? cron ?
        if type(m.__self__) == aiocron.Cron:
            return self.render_cron(m.__self__)
        else:
            return "%s.%s" % (m.__self__.__class__.__name__,
                              m.__name__)

    def render_lambda(self,l):
        return l.__name__

renderer = JobRenderer()

def schedule(loop):
    jobs = {}
    # try to render job in a human-readable way...
    for sch in loop._scheduled:
        if type(sch) != asyncio.events.TimerHandle:
            continue
        if sch._cancelled:
            continue
        try:
            info = renderer.render(sch)
            print(info)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(sch)
    if len(loop._scheduled):
        print()

def find_process(pid):
    g = psutil.process_iter()
    for p in g:
        if p.pid == pid:
            break
    return p


def stats(src_dump):
    pass


def publish_data_version(version,env=None,update_latest=True):
    """
    Update remote files:
    - versions.json: add version to the JSON list
                     or replace if arg version is a list
    - latest.json: update redirect so it points to version
    """
    # TODO: check if a <version>.json exists
    # register version
    versionskey = os.path.join(config.S3_DIFF_FOLDER,"%s.json" % VERSIONS)
    try:
        versions = aws.get_s3_file(versionskey,return_what="content",
                aws_key=config.AWS_KEY,aws_secret=config.AWS_SECRET,
                s3_bucket=config.S3_DIFF_BUCKET)
        versions = json.loads(versions.decode()) # S3 returns bytes
    except (FileNotFoundError,json.JSONDecodeError):
        versions = []
    if type(version) == list:
        versions = version
    else:
        versions.append(version)
    versions = sorted(list(set(versions)))
    aws.send_s3_file(None,versionskey,content=json.dumps(versions),
            aws_key=config.AWS_KEY,aws_secret=config.AWS_SECRET,s3_bucket=config.S3_DIFF_BUCKET,
            content_type="application/json",overwrite=True)

    # update latest
    if type(version) != list and update_latest:
        latestkey = os.path.join(config.S3_DIFF_FOLDER,"%s.json" % LATEST)
        key = None
        try:
            key = aws.get_s3_file(latestkey,return_what="key",
                    aws_key=config.AWS_KEY,aws_secret=config.AWS_SECRET,
                    s3_bucket=config.S3_DIFF_BUCKET)
        except FileNotFoundError:
            pass
        aws.send_s3_file(None,latestkey,content=json.dumps(version),content_type="application/json",
                aws_key=config.AWS_KEY,aws_secret=config.AWS_SECRET,
                s3_bucket=config.S3_DIFF_BUCKET,overwrite=True)
        if not key:
            key = aws.get_s3_file(latestkey,return_what="key",
                    aws_key=config.AWS_KEY,aws_secret=config.AWS_SECRET,
                    s3_bucket=config.S3_DIFF_BUCKET)
        newredir = os.path.join("/",config.S3_DIFF_FOLDER,"%s.json" % version)
        key.set_redirect(newredir)


def _and(*funcs):
    """
    Calls passed functions, one by one. If one fails, then it stops.
    Function should return a asyncio Task. List of one Task only are also permitted.
    Partial can be used to pass arguments to functions.
    Ex: _and(f1,f2,partial(f3,arg1,kw=arg2))
    """
    all_res = []
    func1 = funcs[0]
    func2 = None
    fut1 = func1()
    if type(fut1) == list:
        assert len(fut1) == 1, "Can't deal with list of more than 1 task: %s" % fut1
        fut1 = fut1.pop()
    all_res.append(fut1)
    err = None
    def do(f,cb):
        res = f.result() # consume exception if any
        if cb:
            all_res.extend(_and(cb,*funcs))
    if len(funcs) > 1:
        func2 = funcs[1]
        if len(funcs) > 2:
            funcs = funcs[2:]
        else:
            funcs = []
    fut1.add_done_callback(partial(do,cb=func2))
    return all_res


class HubCommand(str):
    """
    Defines a composite hub commands, that is,
    a new command made of other commands. Useful to define
    shortcuts when typing commands in hub console.
    """
    def __init__(self,cmd):
        self.cmd = cmd
    def __str__(self):
        return "<HubCommand: '%s'>" % self.cmd

