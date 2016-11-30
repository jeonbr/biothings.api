import importlib, threading
import logging
import asyncio, aiocron
import os, pickle, inspect, types
from functools import wraps, partial
import time, datetime

from biothings.utils.mongo import get_src_conn
from biothings.utils.common import timesofar, get_random_string
from biothings import config


def track(func):
    @wraps(func)
    def func_wrapper(*args,**kwargs):
        ptype = args[0] # tracking process or thread ?
        # we're looking for some "pinfo" value (process info) to later
        # reporting. If we can't find any, we'll try our best to figure out
        # what this is about...
        # func is the do_work wrapper, we want the actual partial
        # is first arg a callable (func) or pinfo ?
        if callable(args[1]):
            innerfunc = args[1]
            innerargs = args[2:]
            pinfo = None
        else:
            innerfunc = args[2]
            innerargs = args[3:]
            pinfo = args[1]

        # make sure we can pickle the whole thing (and it's
        # just informative, so stringify is just ok there)
        innerargs = [str(arg) for arg in innerargs]
        if type(innerfunc) == partial:
            fname = innerfunc.func.__name__
        elif type(innerfunc) == types.MethodType:
            fname = innerfunc.__self__.__class__.__name__
        else:
            fname = innerfunc.__name__

        firstarg = innerargs and innerargs[0] or ""
        if not pinfo:
            pinfo = {"category" : None,
                     "source" : None,
                     "step" : None,
                     "description" : "%s %s" % (fname,firstarg)}

        worker = {'func_name' : fname,
                 'args': innerargs, 'kwargs' : kwargs,
                 'started_at': time.time(),
                 'info' : pinfo}
        results = None
        try:
            _id = None
            rnd = get_random_string()
            if ptype == "thread":
                _id = "%s" % threading.current_thread().getName()
            else:
                _id = os.getpid()
            # add random chars: 2 jobs handled by the same slot (pid or thread) 
            # would override filename otherwise
            fn = "%s_%s" % (_id,rnd)
            worker["info"]["id"] = _id
            pidfile = os.path.join(config.RUN_DIR,"%s.pickle" % fn)
            pickle.dump(worker, open(pidfile,"wb"))
            results = func(*args,**kwargs)
        except Exception as e:
            import traceback
            logging.error("err %s\n%s" % (e,traceback.format_exc()))
        finally:
            if os.path.exists(pidfile):
                pass
                # move to "done" dir and register end of execution time 
                os.rename(pidfile,os.path.join(config.RUN_DIR,"done",os.path.basename(pidfile)))
                pidfile = os.path.join(config.RUN_DIR,"done",os.path.basename(pidfile))
                worker = pickle.load(open(pidfile,"rb"))
                worker["duration"] = timesofar(worker["started_at"])
                pickle.dump(worker,open(pidfile,"wb"))
        return results
    return func_wrapper

@track
def do_work(ptype, pinfo=None, func=None, *args, **kwargs):
    # pinfo is optional, and func is not. and args and kwargs must 
    # be after func. just to say func is mandatory, despite what the
    # signature says
    assert func
    # need to wrap calls otherwise multiprocessing could have
    # issue pickling directly the passed func because of some import
    # issues ("can't pickle ... object is not the same as ...")
    return func(*args,**kwargs)

class UnknownResource(Exception):
    pass
class ResourceError(Exception):
    pass
class ManagerError(Exception):
    pass
class ResourceNotFound(Exception):
    pass

class BaseManager(object):

    def __init__(self, job_manager):
        self.register = {}
        self.job_manager = job_manager

    def __repr__(self):
        return "<%s [%d registered]: %s>" % (self.__class__.__name__,len(self.register), sorted(list(self.register.keys())))

    def __getitem__(self,src_name):
        try:
            # as a main-source
            return self.register[src_name]
        except KeyError:
            try:
                # as a sub-source
                main,sub = src_name.split(".")
                srcs = self.register[main]
                # there can be many uploader for one resource (when each is dealing
                # with one specific file but upload to the same collection for instance)
                # so we want to make sure user is aware of this and not just return one
                # uploader when many are needed
                # on the other hand, if only one avail, just return it
                res = []
                for src in srcs:
                    if src.name == sub:
                        res.append(src)
                if len(res) == 0:
                    raise KeyError(src_name)
                else:
                    return res
            except (ValueError,KeyError):
                # nope, can't find it...
                raise KeyError(src_name)



class BaseSourceManager(BaseManager):
    """
    Base class to provide source management: discovery, registration
    Actual launch of tasks must be defined in subclasses
    """

    # define the class manager will look for. Set in a subclass
    SOURCE_CLASS = None

    def __init__(self, job_manager, datasource_path="dataload.sources", *args, **kwargs):
        super(BaseSourceManager,self).__init__(job_manager,*args,**kwargs)
        self.conn = get_src_conn()
        self.default_src_path = datasource_path

    def filter_class(self,klass):
        """
        Gives opportunity for subclass to check given class and decide to
        keep it or not in the discovery process. Returning None means "skip it".
        """
        # keep it by default
        return klass

    def register_classes(self,klasses):
        """
        Register each class in self.register dict. Key will be used
        to retrieve the source class, create an instance and run method from it.
        It must be implemented in subclass as each manager may need to access 
        its sources differently,based on different keys.
        """
        raise NotImplementedError("implement me in sub-class")

    def find_classes(self,src_module,fail_on_notfound=True):
        """
        Given a python module, return a list of classes in this module, matching
        SOURCE_CLASS (must inherit from)
        """
        # try to find a uploader class in the module
        found_one = False
        for attr in dir(src_module):
            something = getattr(src_module,attr)
            if type(something) == type and issubclass(something,self.__class__.SOURCE_CLASS):
                klass = something
                if not self.filter_class(klass):
                    continue
                found_one = True
                logging.debug("Found a class based on %s: '%s'" % (self.__class__.SOURCE_CLASS.__name__,klass))
                yield klass
        if not found_one:
            if fail_on_notfound:
                raise UnknownResource("Can't find a class based on %s in module '%s'" % (self.__class__.SOURCE_CLASS.__name__,src_module))
            return []


    def register_source(self,src_data,fail_on_notfound=True):
        """Register a new data source. src_data can be a module where some classes
        are defined. It can also be a module path as a string, or just a source name
        in which case it will try to find information from default path.
        """
        if isinstance(src_data,str):
            try:
                src_m = importlib.import_module(src_data)
            except ImportError:
                try:
                    src_m = importlib.import_module("%s.%s" % (self.default_src_path,src_data))
                except ImportError:
                    msg = "Can't find module '%s', even in '%s'" % (src_data,self.default_src_path)
                    logging.error(msg)
                    raise UnknownResource(msg)

        elif isinstance(src_data,dict):
            # source is comprised of several other sub sources
            assert len(src_data) == 1, "Should have only one element in source dict '%s'" % src_data
            _, sub_srcs = list(src_data.items())[0]
            for src in sub_srcs:
                self.register_source(src,fail_on_notfound)
            return
        else:
            src_m = src_data
        klasses = self.find_classes(src_m,fail_on_notfound)
        self.register_classes(klasses)

    def register_sources(self, sources):
        assert not isinstance(sources,str), "sources argument is a string, should pass a list"
        self.register.clear()
        for src_data in sources:
            try:
# batch registration, we'll silently ignore not-found sources
                self.register_source(src_data,fail_on_notfound=False)
            except UnknownResource as e:
                logging.info("Can't register source '%s', skip it; %s" % (src_data,e))
                import traceback
                logging.error(traceback.format_exc())


class JobManager(object):

    def __init__(self, loop, process_queue=None, thread_queue=None):
        self.loop = loop
        self.process_queue = process_queue
        self.thread_queue = thread_queue

    def defer_to_process(self, pinfo=None, func=None, *args):
        return self.loop.run_in_executor(self.process_queue,
                partial(do_work,"process",pinfo,func,*args))

    def defer_to_thread(self, pinfo=None, func=None, *args):
        return self.loop.run_in_executor(self.thread_queue,
                partial(do_work,"thread",pinfo,func,*args))

    def submit(self,pfunc,schedule=None):
        """
        Helper to submit and run tasks. Tasks will run async'ly.
        pfunc is a functools.partial
        schedule is a string representing a cron schedule, task will then be scheduled
        accordingly.
        """
        logging.info("Building task: %s" % pfunc)
        if schedule:
            logging.info("Scheduling task %s: %s" % (pfunc,schedule))
            cron = aiocron.crontab(schedule,func=pfunc, start=True, loop=self.loop)
            return cron
        else:
            ff = asyncio.ensure_future(pfunc())
            return ff

