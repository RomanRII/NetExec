#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from nxc.helpers.logger import highlight
from nxc.helpers.misc import identify_target_file
from nxc.parsers.ip import parse_targets
from nxc.parsers.nmap import parse_nmap_xml
from nxc.parsers.nessus import parse_nessus_file
from nxc.cli import gen_cli_args
from nxc.loaders.protocolloader import ProtocolLoader
from nxc.loaders.moduleloader import ModuleLoader
from nxc.servers.http import NXCHTTPServer
from nxc.first_run import first_run_setup
from nxc.context import Context
from nxc.paths import nxc_PATH, DATA_PATH
from nxc.console import nxc_console
from nxc.logger import nxc_logger
from nxc.config import nxc_config, nxc_workspace, config_log, ignore_opsec
from concurrent.futures import ThreadPoolExecutor, as_completed
import asyncio
import nxc.helpers.powershell as powershell
import shutil
import webbrowser
import random
import os
from os.path import exists
from os.path import join as path_join
from sys import exit
import logging
import sqlalchemy
from rich.progress import Progress
from sys import platform

# Increase file_limit to prevent error "Too many open files"
if platform != "win32":
    import resource
    file_limit = list(resource.getrlimit(resource.RLIMIT_NOFILE))
    if file_limit[1] > 10000:
        file_limit[0] = 10000
    else:
        file_limit[0] = file_limit[1]
    file_limit = tuple(file_limit)
    resource.setrlimit(resource.RLIMIT_NOFILE, file_limit)

try:
    import librlers
except:
    print("Incompatible python version, try with another python version or another binary 3.8 / 3.9 / 3.10 / 3.11 that match your python version (python -V)")
    exit(1)

def create_db_engine(db_path):
    db_engine = sqlalchemy.create_engine(f"sqlite:///{db_path}", isolation_level="AUTOCOMMIT", future=True)
    return db_engine


async def start_run(protocol_obj, args, db, targets):
    nxc_logger.debug(f"Creating ThreadPoolExecutor")
    if args.no_progress or len(targets) == 1:
        with ThreadPoolExecutor(max_workers=args.threads + 1) as executor:
            nxc_logger.debug(f"Creating thread for {protocol_obj}")
            _ = [executor.submit(protocol_obj, args, db, target) for target in targets]
    else:
        with Progress(console=nxc_console) as progress:
            with ThreadPoolExecutor(max_workers=args.threads + 1) as executor:
                current = 0
                total = len(targets)
                tasks = progress.add_task(
                    f"[green]Running nxc against {total} {'target' if total == 1 else 'targets'}",
                    total=total,
                )
                nxc_logger.debug(f"Creating thread for {protocol_obj}")
                futures = [executor.submit(protocol_obj, args, db, target) for target in targets]
                for _ in as_completed(futures):
                    current += 1
                    progress.update(tasks, completed=current)


def main():
    first_run_setup(nxc_logger)
    root_logger = logging.getLogger("root")
    args = gen_cli_args()

    if args.verbose:
        nxc_logger.logger.setLevel(logging.INFO)
        root_logger.setLevel(logging.INFO)
    elif args.debug:
        nxc_logger.logger.setLevel(logging.DEBUG)
        root_logger.setLevel(logging.DEBUG)
    else:
        nxc_logger.logger.setLevel(logging.ERROR)
        root_logger.setLevel(logging.ERROR)

    # if these are the same, it might double log to file (two FileHandlers will be added)
    # but this should never happen by accident
    if config_log:
        nxc_logger.add_file_log()
    if hasattr(args, "log") and args.log:
        nxc_logger.add_file_log(args.log)

    nxc_logger.debug(f"Passed args: {args}")

    # FROM HERE ON A PROTOCOL IS REQUIRED
    if not args.protocol:
        exit(1)

    if args.protocol == "ssh":
        if args.key_file:
            if not args.password:
                nxc_logger.fail(f"Password is required, even if a key file is used - if no passphrase for key, use `-p ''`")
                exit(1)

    if args.use_kcache and not os.environ.get("KRB5CCNAME"):
        nxc_logger.error("KRB5CCNAME environment variable is not set")
        exit(1)

    module_server = None
    targets = []
    server_port_dict = {"http": 80, "https": 443, "smb": 445}

    if hasattr(args, "cred_id") and args.cred_id:
        for cred_id in args.cred_id:
            if "-" in str(cred_id):
                start_id, end_id = cred_id.split("-")
                try:
                    for n in range(int(start_id), int(end_id) + 1):
                        args.cred_id.append(n)
                    args.cred_id.remove(cred_id)
                except Exception as e:
                    nxc_logger.error(f"Error parsing database credential id: {e}")
                    exit(1)

    if hasattr(args, "target") and args.target:
        for target in args.target:
            if exists(target) and os.path.isfile(target):
                target_file_type = identify_target_file(target)
                if target_file_type == "nmap":
                    targets.extend(parse_nmap_xml(target, args.protocol))
                elif target_file_type == "nessus":
                    targets.extend(parse_nessus_file(target, args.protocol))
                else:
                    with open(target, "r") as target_file:
                        for target_entry in target_file:
                            targets.extend(parse_targets(target_entry.strip()))
            else:
                targets.extend(parse_targets(target))

    # The following is a quick hack for the powershell obfuscation functionality, I know this is yucky
    if hasattr(args, "clear_obfscripts") and args.clear_obfscripts:
        shutil.rmtree(os.path.expanduser("~/.nxc/obfuscated_scripts/"))
        os.mkdir(os.path.expanduser("~/.nxc/obfuscated_scripts/"))
        nxc_logger.success("Cleared cached obfuscated PowerShell scripts")

    if hasattr(args, "obfs") and args.obfs:
        powershell.obfuscate_ps_scripts = True

    nxc_logger.debug(f"Protocol: {args.protocol}")
    p_loader = ProtocolLoader()
    protocol_path = p_loader.get_protocols()[args.protocol]["path"]
    nxc_logger.debug(f"Protocol Path: {protocol_path}")
    protocol_db_path = p_loader.get_protocols()[args.protocol]["dbpath"]
    nxc_logger.debug(f"Protocol DB Path: {protocol_db_path}")

    protocol_object = getattr(p_loader.load_protocol(protocol_path), args.protocol)
    nxc_logger.debug(f"Protocol Object: {protocol_object}")
    protocol_db_object = getattr(p_loader.load_protocol(protocol_db_path), "database")
    nxc_logger.debug(f"Protocol DB Object: {protocol_db_object}")

    db_path = path_join(nxc_PATH, "workspaces", nxc_workspace, f"{args.protocol}.db")
    nxc_logger.debug(f"DB Path: {db_path}")

    db_engine = create_db_engine(db_path)

    db = protocol_db_object(db_engine)

    # with the new nxc/config.py this can be eventually removed, as it can be imported anywhere
    setattr(protocol_object, "config", nxc_config)

    if args.module or args.list_modules:
        loader = ModuleLoader(args, db, nxc_logger)
        modules = loader.list_modules()

    if args.list_modules:
        for name, props in sorted(modules.items()):
            if args.protocol in props["supported_protocols"]:
                nxc_logger.display(f"{name:<25} {props['description']}")
        exit(0)
    elif args.module and args.show_module_options:
        for module in args.module:
            nxc_logger.display(f"{module} module options:\n{modules[module]['options']}")
        exit(0)
    elif args.module:
        nxc_logger.debug(f"Modules to be Loaded: {args.module}, {type(args.module)}")
        for m in map(str.lower, args.module):
            if m not in modules:
                nxc_logger.error(f"Module not found: {m}")
                exit(1)

            nxc_logger.debug(f"Loading module {m} at path {modules[m]['path']}")
            module = loader.init_module(modules[m]["path"])

            if not module.opsec_safe:
                if ignore_opsec:
                    nxc_logger.debug(f"ignore_opsec is set in the configuration, skipping prompt")
                    nxc_logger.display(f"Ignore OPSEC in configuration is set and OPSEC unsafe module loaded")
                else:
                    ans = input(
                        highlight(
                            "[!] Module is not opsec safe, are you sure you want to run this? [Y/n] For global configuration, change ignore_opsec value to True on ~/nxc/nxc.conf",
                            "red",
                        )
                    )
                    if ans.lower() not in ["y", "yes", ""]:
                        exit(1)

            if not module.multiple_hosts and len(targets) > 1:
                ans = input(
                    highlight(
                        "[!] Running this module on multiple hosts doesn't really make any sense, are you sure you want to continue? [Y/n] ",
                        "red",
                    )
                )
                if ans.lower() not in ["y", "yes", ""]:
                    exit(1)

            if hasattr(module, "on_request") or hasattr(module, "has_response"):
                if hasattr(module, "required_server"):
                    args.server = module.required_server

                if not args.server_port:
                    args.server_port = server_port_dict[args.server]

                # loading a module server multiple times will obviously fail
                try:
                    context = Context(db, nxc_logger, args)
                    module_server = NXCHTTPServer(
                        module,
                        context,
                        nxc_logger,
                        args.server_host,
                        args.server_port,
                        args.server,
                    )
                    module_server.start()
                    protocol_object.server = module_server.server
                except Exception as e:
                    nxc_logger.error(f"Error loading module server for {module}: {e}")

            nxc_logger.debug(f"proto_object: {protocol_object}, type: {type(protocol_object)}")
            nxc_logger.debug(f"proto object dir: {dir(protocol_object)}")
            # get currently set modules, otherwise default to empty list
            current_modules = getattr(protocol_object, "module", [])
            current_modules.append(module)
            setattr(protocol_object, "module", current_modules)
            nxc_logger.debug(f"proto object module after adding: {protocol_object.module}")

    if hasattr(args, "ntds") and args.ntds and not args.userntds:
        ans = input(
            highlight(
                "[!] Dumping the ntds can crash the DC on Windows Server 2019. Use the option --user <user> to dump a specific user safely or the module -M ntdsutil [Y/n] ",
                "red",
            )
        )
        if ans.lower() not in ["y", "yes", ""]:
            exit(1)

    try:
        asyncio.run(start_run(protocol_object, args, db, targets))
    except KeyboardInterrupt:
        nxc_logger.debug("Got keyboard interrupt")
    finally:
        if module_server:
            module_server.shutdown()
        db_engine.dispose()


if __name__ == "__main__":
    main()