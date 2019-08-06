# executor.py
#
# Authors:
#   - Coumes Quentin <coumes.quentin@gmail.com>
import logging
import os
import tarfile
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from django.conf import settings
from django_http_exceptions import HTTPExceptions
from docker.models.containers import Container
from timeout_decorator import timeout_decorator

from sandbox import utils
from .container import Sandbox
from .enums import SandboxErrCode


logger = logging.getLogger(__name__)



class Command:
    """Use to wrap bash commands."""
    
    
    def __init__(self, command: str, timeout: float = settings.EXECUTE_TIMEOUT, environ=None):
        if environ is None:
            environ = {}
        if command.startswith("-"):
            self.command = command[1:]
            self.ignore_failure = True
        else:
            self.command = command
            self.ignore_failure = False
        self.environ = environ if environ is not None else {}
        self.timeout = timeout
    
    
    def __repr__(self):
        return f"<executor.Command '{self.command}' timeout={self.timeout}>"
    
    
    __str__ = __repr__
    
    
    @staticmethod
    def _check(d: dict) -> bool:
        """Returns True if <d> is a valid representation of a command, False otherwise.
        
        Check that:
            - 'command' is present and is a string.
            - if 'timeout' is present, it is either an integer or a float."""
        return all((
            'command' in d and isinstance(d["command"], str),
            "timeout" not in d or isinstance(d["timeout"], (int, float)),
        ))
    
    
    @classmethod
    def from_config(cls, config: dict) -> List['Command']:
        """Extract commands from the config dictionary, returning a list of Commands."""
        if 'commands' not in config:
            raise HTTPExceptions.BAD_REQUEST.with_content("Missing field 'commands' in config")
        
        if not isinstance(config["commands"], list):
            raise HTTPExceptions.BAD_REQUEST.with_content(
                f'commands must be a list, not {type(config["commands"])}')
        
        environ = utils.parse_environ(config)
        
        commands = list()
        for c in config["commands"]:
            if isinstance(c, dict) and cls._check(c):
                commands.append(Command(environ=environ, **c))
            elif isinstance(c, str):
                commands.append(Command(c, environ=environ))
            else:
                raise HTTPExceptions.BAD_REQUEST.with_content(f"Command badly formatted : '{c}'")
        
        return commands
    
    
    def execute(self, container: Container) -> Tuple[bool, dict]:
        """Execute the command on the given container."""
        start = time.time()
        
        try:
            timeout = timeout_decorator.timeout(self.timeout, use_signals=False)
            exec_run = timeout(container.exec_run)
            exit_code, output = exec_run(
                ["bash", "-c", self.command], environment=self.environ, demux=True)
            stdout, stderr = ("" if out is None else out.decode().strip() for out in output)
        except timeout_decorator.TimeoutError:
            exit_code = SandboxErrCode.TIMEOUT
            stdout = ""
            stderr = f"Sandbox timed out after {self.timeout} seconds\n"
        except Exception:  # pragma: no cover
            logger.exception(f"An error occurred while executing the command '{self.command}'")
            exit_code = SandboxErrCode.UNKNOWN
            stdout = ""
            stderr = "An unknown error occurred on the sandbox\n"
        
        result = {
            "command":   self.command,
            "exit_code": exit_code,
            "stdout":    stdout,
            "stderr":    stderr,
            "time":      time.time() - start,
        }
        
        if exit_code < 0:
            status = False
        elif self.ignore_failure:
            status = True
        else:
            status = (exit_code == 0)
        
        return status, result



class Executor:
    """This class provide methods to execute bash commands."""
    
    
    def __init__(self, commands: List[Command], sandbox: Sandbox, env_uuid: str = None,
                 result: str = None, save: bool = False):
        self.commands = commands
        self.sandbox = sandbox
        self.env_uuid = env_uuid
        self.result_path = result
        self.save = save
        
        if env_uuid is not None:
            self.env_path = os.path.join(settings.ENVIRONMENT_ROOT, f"{env_uuid}.tgz")
        else:
            self.env_path = None
    
    
    def _move_env_to_container(self):
        """Send the tar to the Docker and untar it inside the Docker"""
        start = time.time()
        
        with tarfile.open(self.env_path, "r:gz") as tar:
            tar.extractall(self.sandbox.envpath)
        
        logger.debug(f"Moving environment to container took : {time.time() - start} seconds")
    
    
    def _get_result(self) -> Optional[str]:
        """Return the content of /home/docker/<path> if found, an empty string otherwise."""
        start = time.time()
        with open(os.path.join(self.sandbox.envpath, self.result_path), encoding="UTF-8") as f:
            content = f.read()
        logger.debug(f"Getting result from container took : {time.time() - start} seconds")
        return content.strip()
    
    
    def execute(self) -> dict:
        """Execute each commands in the container."""
        start = time.time()
        
        if self.env_path is not None:
            self._move_env_to_container()
        
        execution = list()
        
        for command in self.commands:
            status, exec_result = command.execute(self.sandbox.container)
            execution.append(exec_result)
            if not status:
                status = exec_result["exit_code"]
                break
        else:
            status = 0

        result = None
        if self.result_path is not None:
            try:
                result = self._get_result()
            except FileNotFoundError:
                status = SandboxErrCode.RESULT_NOT_FOUND
            except UnicodeDecodeError:
                status = SandboxErrCode.RESULT_NOT_UTF8
        
        response = {
            "status":     status,
            "execution":  execution,
            "total_time": time.time() - start,
        }
        
        if self.env_uuid is not None and self.save:
            expire = datetime.now() + timedelta(seconds=settings.ENVIRONMENT_EXPIRATION)
            response["environment"] = self.env_uuid
            response["expire"] = expire.isoformat()
            os.remove(self.env_path)
            threading.Thread(target=self.sandbox.extract_env, args=(self.env_uuid,)).start()
        elif self.env_uuid is not None and not self.save:
            os.remove(self.env_path)
        
        if result is not None:
            response["result"] = result
        
        return response
