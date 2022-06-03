import docker
import os
import shutil
import time
import json

import config

from threading import Thread

from uuid import uuid1

from solution_checker.utils import create_file, files_to_tar
from solution_checker.docker_utils import remove_container, get_file_from_container

from linter.linter import lint_dict, lint_errors_to_str


STATUS_OK = 0
STATUS_CHECKING = 1
STATUS_BUILD_ERROR = 2
STATUS_RUNTIME_ERROR = 3
STATUS_CHECK_ERROR = 4
STATUS_RUNTIME_TIMEOUT = 6
STATUS_BUILD_TIMEOUT = 7
STATUS_LINT_ERROR = 8
STATUS_DRAFT = 9

# todo: receive check timeout from backend
BUILD_TIMEOUT = config.DEFAULT_BUILD_TIMEOUT  # in seconds
RUNTIME_TIMEOUT = config.DEFAULT_TEST_TIMEOUT  # in seconds


class CheckResult:
    def __init__(self,
                 check_time: float = 0.0,
                 build_time: float = 0.0,
                 check_result: int = -1,
                 check_message: str = '',
                 tests_passed: int = 0,
                 tests_total: int = 0,
                 lint_success: bool = False,
                 ):
        self.check_time = check_time  # todo: rename to test_time
        self.build_time = build_time
        self.check_result = check_result  # todo: rename to status
        self.check_message = check_message  # todo: rename to message
        self.tests_passed = tests_passed
        self.tests_total = tests_total
        self.lint_success = lint_success

    def json(self) -> str:
        json_data = {}
        for key, value in self.__dict__.items():
            key_split = key.split('_')
            new_key = key_split[0] + ''.join(word.capitalize() for word in key_split[1:])
            json_data[new_key] = value
        return json.dumps(json_data)


class DockerBuildThread(Thread):
    result = None

    def __init__(self, client, container, source_path):
        super().__init__()
        self.client = client
        self.container = container
        self.source_path = source_path

    def run(self):
        build_command = 'make build'
        build_result = self.container.exec_run(build_command, workdir=self.source_path)
        self.result = build_result

    def terminate(self):
        self.container.kill()


class DockerTestThread(Thread):
    result = None

    def __init__(self, client, container, source_path, stdin_file_path, tests):
        super().__init__()
        self.client = client
        self.container = container
        self.source_path = source_path
        self.stdin_file_path = stdin_file_path
        self.tests = tests

    def run(self):
        tests_passed = 0

        result = CheckResult(tests_total=len(self.tests))

        for test in self.tests:
            stdin, expected = test[0], test[1]

            create_file(self.stdin_file_path, '{}\n'.format(stdin))
            source_path = '/root/source'
            input_file_path = '/root/input/input.txt'
            output_file_path = source_path + '/output.txt'

            run_command = '/bin/bash -c "rm -f {output_fpath} && cat {input_fpath} | make -s ARGS=\'{input_fpath} {output_fpath}\' run"'.format(
                input_fpath=input_file_path,
                output_fpath=output_file_path
            )
            execute_result = self.container.exec_run(run_command, workdir=source_path, environment={
                'ARGS': '{} {}'.format(input_file_path, output_file_path),
                'input_path': input_file_path,
                'output_path': output_file_path
            })

            self.container = self.client.containers.get(self.container.id)
            if self.container.status == 'exited':
                result.check_result = STATUS_CHECK_ERROR
                result.tests_passed = tests_passed
                self.result = result
                return

            stdout = execute_result.output.decode()
            if execute_result.exit_code != 0:
                result.check_result = STATUS_RUNTIME_ERROR
                result.check_message = stdout
                self.result = result
                return

            fout = get_file_from_container(self.container, output_file_path)
            answer = fout if fout is not None else stdout

            # it's a practice to add \n at the end of output, but usually tests don't have it
            if len(answer) > 0 and answer[-1] == '\n' and expected[-1] != '\n':
                answer = answer[0:-1]

            if answer != expected:
                msg = 'For "{}" expected "{}", but got "{}"'.format(test[0], test[1], answer)
                result.check_result = STATUS_CHECK_ERROR
                result.check_message = msg
                result.tests_passed = tests_passed
                self.result = result

                return

            tests_passed += 1

        result.check_result = STATUS_OK
        result.tests_passed = tests_passed
        self.result = result

    def terminate(self):
        self.container = self.client.containers.get(self.container.id)
        self.container.kill()


def build_solution(client, container):
    build_thread = DockerBuildThread(client, container, '/root/source')
    start_time = time.time()
    build_thread.start()
    build_thread.join(BUILD_TIMEOUT)
    build_time = round(time.time() - start_time, 4)

    compile_result = build_thread.result
    if compile_result is None:
        build_thread.terminate()
        # waiting for container to stop and then thread will exit
        build_thread.join()

    return compile_result, build_time


def test_solution(client, container, stdin_file_path, tests):
    test_thread = DockerTestThread(client, container, '/root/source', stdin_file_path, tests)
    start_time = time.time()
    test_thread.start()
    test_thread.join(RUNTIME_TIMEOUT * len(tests))
    test_time = round(time.time() - start_time, 4)

    if test_thread.result is None:
        test_thread.terminate()
        # waiting for container to stop and then thread will exit
        test_thread.join()

    return test_thread.result, test_time


def check_solution(client, container, stdin_file_path, tests, need_to_build=True):
    build_time = .0
    if need_to_build:
        build_result, build_time = build_solution(client, container)

        if build_result is None:
            return CheckResult(
                build_time=build_time,
                check_result=STATUS_BUILD_TIMEOUT,
                tests_total=len(tests)
            )

        if build_result.exit_code != 0:
            msg = build_result.output.decode()
            return CheckResult(
                build_time=build_time,
                check_result=STATUS_BUILD_ERROR,
                check_message=msg,
                tests_total=len(tests)
            )

    test_result, test_time = test_solution(client, container, stdin_file_path, tests)

    if test_result is None:
        test_result = CheckResult(check_result=STATUS_RUNTIME_TIMEOUT)

    result = test_result
    result.check_time = test_time
    result.build_time = build_time

    return result


def check_task_multiple_files(source_code: dict, tests: list) -> CheckResult:
    makefile = source_code.get('Makefile', None)
    if makefile is None:
        return CheckResult(check_result=STATUS_BUILD_ERROR, check_message='No Makefile found!')
    if makefile.find('run:') == -1:
        return CheckResult(check_result=STATUS_BUILD_ERROR, check_message='Makefile must contain "run:"')

    need_to_build = makefile.find('build:') != -1

    try:
        tar_source = files_to_tar(source_code, 'source/')
    except Exception:
        raise Exception('Unable to parse source code!')

    solution_dir = str(uuid1())
    solution_path = os.path.join(os.getcwd(), 'solutions', solution_dir)
    solution_path_input = os.path.join(solution_path, 'input')
    stdin_file_path = os.path.join(solution_path_input, 'input.txt')

    client = docker.from_env()

    container = client.containers.run('liokorcode_checker', detach=True, tty=True, volumes={
            solution_path_input: {
                'bind': '/root/input',
                'mode': 'ro'
            },
        },
        network_disabled=True,
        mem_limit='64m',
    )

    try:
        container.put_archive('/root', tar_source.read())
    except Exception:
        remove_container(client, container.id)
        raise Exception('Unable to create requested filesystem!')

    result = check_solution(client, container, stdin_file_path, tests, need_to_build)

    if result.check_result == STATUS_OK:
        lint_errors = lint_dict(source_code)
        str_lint = lint_errors_to_str(lint_errors)

        result.lint_success = len(str_lint) == 0
        if not result.lint_success:
            result.check_message += '\n{}'.format(str_lint)

    shutil.rmtree(solution_path)
    remove_container(client, container.id)

    return result
