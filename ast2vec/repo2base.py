import json
import logging
import multiprocessing
import os
from queue import Queue
import re
import shutil
import subprocess
import tempfile
import threading

import asdf
from bblfsh import BblfshClient
from bblfsh.launcher import ensure_bblfsh_is_running
import Stemmer

from ast2vec.meta import ARRAY_COMPRESSION


class LinguistFailedError(Exception):
    """
    Raised when we fail to classify the source files.
    """
    pass


class Repo2Base:
    """
    Base class for repsitory features extraction. Abstracts from
    `Babelfish <https://doc.bblf.sh/>`_ and source code identifier processing.
    """
    MODEL_CLASS = None  #: Must be defined in the children.
    NAME_BREAKUP_RE = re.compile(r"[^a-zA-Z]+")  #: Regexp to split source code identifiers.
    STEM_THRESHOLD = 6  #: We do not stem splitted parts shorter than or equal to this size.
    MAX_TOKEN_LENGTH = 256  #: We cut identifiers longer than thi value.
    DEFAULT_BBLFSH_TIMEOUT = 10  #: Longer requests are dropped.
    MAX_FILE_SIZE = 200000

    def __init__(self, tempdir=None, linguist=None, log_level=logging.INFO,
                 bblfsh_endpoint=None, timeout=DEFAULT_BBLFSH_TIMEOUT):
        self._log = logging.getLogger("repo2" + self.MODEL_CLASS.NAME)
        self._log.setLevel(log_level)
        self._stemmer = Stemmer.Stemmer("english")
        self._stemmer.maxCacheSize = 0
        self._stem_threshold = self.STEM_THRESHOLD
        self._tempdir = tempdir
        self._linguist = linguist
        if self._linguist is None:
            self._linguist = shutil.which("enry", path=os.getcwd())
        if self._linguist is None:
            self._linguist = "enry"
        full_path = shutil.which(self._linguist)
        if not full_path:
            raise FileNotFoundError("%s was not found. Install it: python3 -m ast2vec enry" %
                                    self._linguist)
        with open(full_path, "rb") as fin:
            self._is_enry = fin.read(15) != b"#!/usr/bin/ruby"
        self._bblfsh = [BblfshClient(bblfsh_endpoint or "0.0.0.0:9432")
                        for _ in range(multiprocessing.cpu_count())]
        self._timeout = timeout

    def convert_repository(self, url_or_path):
        """
        Queries bblfsh for the UASTs and produces smth useful from them.

        :param url_or_path: Fiel system path to the repository or a URL to clone.
        :return: Some object(s) which are returned from convert_uasts().
        """
        temp = not os.path.exists(url_or_path)
        if temp:
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            target_dir = tempfile.mkdtemp(
                prefix="repo2nbow-", dir=self._tempdir)
            self._log.info("Cloning from %s...", url_or_path)
            try:
                subprocess.check_call(
                    ["git", "clone", "--depth=1", url_or_path, target_dir],
                    env=env, stdin=subprocess.DEVNULL)
            except Exception as e:
                shutil.rmtree(target_dir, ignore_errors=True)
                raise e from None
        else:
            target_dir = url_or_path
        try:
            self._log.info("Classifying the files...")
            classified = self._classify_files(target_dir)
            self._log.info("Result: %s", {k: len(v) for k, v in classified.items()})
            self._log.info("Fetching and processing UASTs...")

            def uast_generator():
                queue_in = Queue()
                queue_out = Queue()

                def thread_loop(thread_index):
                    while True:
                        task = queue_in.get()
                        if task is None:
                            break
                        try:
                            filename, language = task

                            # Check if filename is symlink
                            if os.path.islink(filename):
                                filename = os.readlink(filename)

                            size = os.stat(filename).st_size
                            if size > self.MAX_FILE_SIZE:
                                self._log.warning("%s is too big - %d", filename, size)
                                queue_out.put_nowait(None)
                                continue

                            uast = self._bblfsh[thread_index].parse_uast(
                                filename, language=language, timeout=self._timeout)
                            if uast is None:
                                self._log.warning("bblfsh timed out on %s", filename)
                            queue_out.put_nowait(uast)
                        except:
                            self._log.exception(
                                "Error while processing %s", task)
                            queue_out.put_nowait(None)

                pool = [threading.Thread(target=thread_loop, args=(i,),
                                         name="%s@%d" % (url_or_path, i))
                        for i in range(multiprocessing.cpu_count())]
                for thread in pool:
                    thread.start()
                tasks = 0
                empty = True
                lang_list = ("Python", "Java")
                for lang, files in classified.items():
                    # FIXME(vmarkovtsev): remove this hardcode when https://github.com/bblfsh/server/issues/28 is resolved # nopep8
                    if lang not in lang_list:
                        continue
                    for f in files:
                        tasks += 1
                        empty = False
                        queue_in.put_nowait(
                            (os.path.join(target_dir, f), lang))
                report_interval = max(1, tasks // 100)
                for _ in pool:
                    queue_in.put_nowait(None)
                while tasks > 0:
                    result = queue_out.get()
                    if result is not None:
                        yield result
                    tasks -= 1
                    if tasks % report_interval == 0:
                        self._log.info("%s pending tasks: %d", url_or_path,
                                       tasks)
                for thread in pool:
                    thread.join()

                if empty:
                    self._log.warning("No files were processed")

            return self.convert_uasts(uast_generator())
        finally:
            if temp:
                shutil.rmtree(target_dir)

    def convert_uast(self, uast):
        return self.convert_uasts([uast])

    def convert_uasts(self, uast_generator):
        raise NotImplementedError()

    def _classify_files(self, target_dir):
        target_dir = os.path.abspath(target_dir)
        cmdline = [self._linguist]
        if self._is_enry:
            cmdline += ["-json", target_dir]
        else:
            cmdline += [target_dir, "--json"]
        try:
            bjson = subprocess.check_output(cmdline)
        except subprocess.CalledProcessError:
            raise LinguistFailedError() from None
        classified = json.loads(bjson.decode("utf-8"))
        return classified

    def _process_token(self, token):
        for word in self._split(token):
            yield self._stem(word)

    def _stem(self, word):
        if len(word) <= self._stem_threshold:
            return word
        return self._stemmer.stemWord(word)

    @classmethod
    def _split(cls, token):
        token = token.strip()[:cls.MAX_TOKEN_LENGTH]
        prev_p = [""]

        def ret(name):
            r = name.lower()
            if len(name) >= 3:
                yield r
                if prev_p[0]:
                    yield prev_p[0] + r
                    prev_p[0] = ""
            else:
                prev_p[0] = r

        for part in cls.NAME_BREAKUP_RE.split(token):
            if not part:
                continue
            prev = part[0]
            pos = 0
            for i in range(1, len(part)):
                this = part[i]
                if prev.islower() and this.isupper():
                    yield from ret(part[pos:i])
                    pos = i
                elif prev.isupper() and this.islower():
                    if 0 < i - 1 - pos <= 3:
                        yield from ret(part[pos:i - 1])
                        pos = i - 1
                    elif i - 1 > pos:
                        yield from ret(part[pos:i])
                        pos = i
                prev = this
            last = part[pos:]
            if last:
                yield from ret(last)


class Transformer:
    """
    Base class for transformers
    """

    def transform(self, *args, **kwargs):
        return NotImplementedError()


class RepoTransformer(Transformer):
    WORKER_CLASS = None
    DEFAULT_NUM_PROCESSES = 2

    def __init__(self, num_processes=DEFAULT_NUM_PROCESSES, **args):
        super(RepoTransformer, self).__init__()
        self._args = args
        self._log = logging.getLogger(self.WORKER_CLASS.MODEL_CLASS.NAME + "_transformer")
        self._num_processes = num_processes

    @property
    def num_processes(self):
        return self._num_processes

    @num_processes.setter
    def num_processes(self, value):
        if not isinstance(value, int):
            raise TypeError("num_processes must be an integer")
        self._num_processes = value

    @classmethod
    def process_entry(cls, url_or_path, args, outdir):
        """
        Invokes process_repo() in a separate process. The reason we do this is that grpc
        starts hanging background threads for every channel which poll(). Those threads
        do not exit when the channel is destroyed. It is fine for a single repository, but
        quickly hits the system limit in case of many.

        This method is intended for the batch processing.

        :param url_or_path: File system path or a URL to clone.
        :param args: :class:`dict`-like container with the arguments to cls().
        :param outdir: The output directory.
        :return:
        """
        pid = os.fork()
        if pid == 0:
            outfile = cls.prepare_filename(url_or_path, outdir)
            cls(**args).process_repo(url_or_path, outfile)
            import sys
            sys.exit()
        else:
            os.waitpid(pid, 0)

    @classmethod
    def prepare_filename(cls, repo, output):
        """
        Remove prefixes from the repo name, so later it can be used to create
        file for each repository + replace slashes ("/") with sharps ("#").

        :param repo: name of repository
        :param output: output folder
        :return: converted repository name (removed "http://", etc.)
        """
        prefixes = ["https://", "http://"]
        for prefix in prefixes:
            if repo.startswith(prefix):
                repo_name = repo[len(prefix):]
                break
        else:
            repo_name = repo
        outfile = os.path.join(output, "%s_%s.asdf" % (
            cls.WORKER_CLASS.MODEL_CLASS.NAME, repo_name.replace("/", "&")))
        return outfile

    def process_repo(self, url_or_path, output):
        """
        Pipeline for a single repository:

        1. Initialize the implementation class instance.
        2. Extract vocabulary and co-occurrence matrix from the repository.
        3. Save the result as ASDF.

        :param url_or_path: Repository URL or file system path.
        :param output: Path to file where to store the result.
        """
        repo2 = self.WORKER_CLASS(**self._args)
        try:
            result = repo2.convert_repository(url_or_path)
            try:
                tree = self.result_to_tree(result)
            except ValueError:
                self._log.warning("Not written: %s", output)
                return
            self._log.info("Writing %s...", output)
            asdf.AsdfFile(tree).write_to(
                output, all_array_compression=ARRAY_COMPRESSION)
        except subprocess.CalledProcessError as e:
            self._log.error("Failed to clone %s: %s", url_or_path, e)
        except:
            self._log.exception(
                "Unhandled error in %s.process_repo() at %s." % (
                    type(self).__name__, url_or_path))

    def transform(self, repos, output, num_processes=None):
        """
        Extracts co-occurrence matrices & list of tokens for each repository ->
        saves to the output directory.

        :param repos: "repos" is the list of repository URLs or paths or \
                  files with repository URLS or paths.
        :param output: "output" is the output directory where to store the \
                        results.
        :param num_processes: number of processes to use, if negative - use all \
               CPUs.
        :return: None
        """
        if num_processes is None:
            num_processes = self.num_processes
        if num_processes < 0:
            num_processes = multiprocessing.cpu_count()

        inputs = []

        if isinstance(repos, str):
            repos = [repos]

        for repo in repos:
            # check if it's a text file
            if os.path.isfile(repo):
                with open(repo) as f:
                    inputs.extend(l.strip() for l in f)
            else:
                inputs.append(repo)

        os.makedirs(output, exist_ok=True)

        with multiprocessing.Pool(processes=num_processes) as pool:
            pool.starmap(type(self).process_entry,
                         zip(inputs, [self._args] * len(inputs),
                             [output] * len(inputs)))

    def result_to_tree(self, result):
        """
        Converts the "result" object from parse_uasts() to a tree-like structure for ASDF.

        :param result: The object returned from parse_uasts().
        :return: :class:`dict` with "meta" and some custom nodes.
        """
        raise NotImplementedError


def ensure_bblfsh_is_running_noexc():
    """
    Launches the Babelfish server, if it is possible and needed.

    :return: None
    """
    try:
        ensure_bblfsh_is_running()
    except:
        log = logging.getLogger("bblfsh")
        message = "Failed to ensure that the Babelfish server is running."
        if log.isEnabledFor(logging.DEBUG):
            log.exception(message)
        else:
            log.warning(message)


def _sanitize_kwargs(args, *blacklist):
    payload_args = getattr(args, "__dict__", args).copy()
    blacklist += ("output", "command", "handler")
    for arg in blacklist:
        if arg in payload_args:
            del payload_args[arg]
    return payload_args


def repo2_entry(args, payload_class):
    """
    Invokes payload_class(\*\*args).process_repo() on the specified repository.

    :param args: :class:`argparse.Namespace` with "repository" and "output". \
                 "repository" is a file system path or a URL. "output" is the path \
                 to the file with the resulting model.
    :param payload_class: :class:`Transformer` inheritor to call.
    :return: None
    """
    ensure_bblfsh_is_running_noexc()
    payload_args = _sanitize_kwargs(args, "repository")
    payload_class(**payload_args).process_repo(args.repository, args.output)


def repos2_entry(args, payload_class):
    """
    Invokes payload_class(\*\*args).transform() for every repository in parallel processes.

    :param args: :class:`argparse.Namespace` with "input" and "output". \
                 "input" is the list of repository URLs or paths or files \
                 with repository URLS or paths. "output" is the output \
                 directory where to store the results.
    :param payload_class: :class:`Transformer` inheritor to call.
    :return: None
    """
    ensure_bblfsh_is_running_noexc()
    payload_args = _sanitize_kwargs(args, "input")
    payload_class(**payload_args).transform(args.input, args.output)
