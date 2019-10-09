from os import _exit
from threading import Thread

from ar3.core.ssh import SSH
from ar3.core.wmi import WmiCon
from ar3.core.rpc import RpcCon
from ar3.core.smb import SmbCon
from ar3.logger import highlight
from ar3.core.winrm import WINRM
from ar3.helpers import powershell
from ar3.core.wmiexec import WMIEXEC
from ar3.core.smbexec import SMBEXEC
from ar3.core.atexec import TSCHEXEC
from ar3.helpers.misc import slack_post
from ar3.ops.enum.polenum import SAMRDump
from ar3.ops.enum.share_finder import share_finder
from ar3.modules import get_module_class, populate_mod_args
from ar3.ops.enum.code_execution import ExecutionTimeout

def requires_admin(func):
    def _decorator(con, *args, **kwargs):
        if not con.admin:
            return False
        return func(con, *args, **kwargs)
    return _decorator

def smb_login(args, loggers, host, db, lockout_obj):
    try:
        con = SmbCon(args, loggers, host, db)
        con.create_smb_con()
        return con
    except Exception as e:
        lockout_obj.failed_login(host, str(e))
        return False

def ssh_login(args, loggers, host, db, lockout_obj):
    try:
        con = SSH(args, loggers, host, db)
        con.create_ssh_con()
        con.host_info()
        con.isAdmin()
        con.signing = 'N/A'
        con.smbv1   = 'N/A'
        return con
    except Exception as e:
        lockout_obj.failed_login(host, str(e))
        return False

def password_policy(con, args, db_obj, loggers):
    ppol = SAMRDump(con, args.debug, loggers['console'])
    ppol.dump(con.ip)
    if ppol.threshold:
        if ppol.threshold == "None":
            loggers['console'].status('Lockout threshold: None, setting threshold to 99 in database for {}'.format(con.domain))
            db_obj.update_domain(con.domain, 99)
        else:
            loggers['console'].status('Lockout threshold detected, setting threshold to {} in database for {}'.format(ppol.threshold, con.domain))
            db_obj.update_domain(con.domain, ppol.threshold)
    else:
        raise Exception('Enumerating password policy failed')


@requires_admin
def code_execution(con, args, target, loggers, config_obj, payload, return_data=False):
    # Implement Execution Method
    if args.exec_method.lower() == 'wmiexec':
        executioner = WMIEXEC(loggers['console'], target, args, con, share_name=args.fileless_sharename)
    elif args.exec_method.lower() == 'smbexec':
        executioner = SMBEXEC(loggers['console'], target, args, con, share_name=args.fileless_sharename)
    elif args.exec_method.lower() == 'atexec':
        executioner = TSCHEXEC(loggers['console'], target, args, con, share_name=args.fileless_sharename)
    elif args.exec_method.lower() == 'winrm':
        executioner = WINRM(loggers['console'], target, args, con, share_name=False)
    elif args.exec_method.lower() == 'ssh':
        executioner = con
    # Log action to file
    loggers[args.mode].info("Code Execution\t{}\t{}\\{}\t{}".format(target, args.domain, args.user, payload))

    # Spawn thread for code execution timeout
    timer = ExecutionTimeout(executioner, payload)
    exe_thread = Thread(target=timer.execute)
    exe_thread.start()
    exe_thread.join(args.timeout+5)
    exe_thread.running = False

    # CMD Output
    if args.slack and config_obj.SLACK_API and config_obj.SLACK_CHANNEL:
        post_data = "[Host: {}]\t[User:{}]\t[Command:{}]\r\n{}".format(con.host, args.user, payload, timer.result)
        slack_post(config_obj.SLACK_API, config_obj.SLACK_CHANNEL, post_data)

    # Return to module not print
    if return_data:
        return timer.result

    for line in timer.result.splitlines():
        loggers['console'].info([con.host, con.ip, args.exec_method.upper(), line])

@requires_admin
def ps_execution(con,args,target,loggers,config_obj):
    try:
        cmd = powershell.create_ps_command(args.ps_execute, loggers['console'], force_ps32=args.force_ps32, no_obfs=args.no_obfs, server_os=con.os)
        result = code_execution(con, args, target, loggers, config_obj, cmd, return_data=True)
        for line in result.splitlines():
            loggers['console'].info([con.host, con.ip, args.exec_method.upper(), line])
    except Exception as e:
        loggers['console'].debug([con.host, con.ip, args.exec_method.upper(), str(e)])

@requires_admin
def extract_sam(con, args, target, loggers):
    loggers[args.mode].info("Extract SAM\t{}\t{}\\{}".format(target, args.domain, args.user))
    con.sam()

@requires_admin
def extract_ntds(con, args, target, loggers):
    loggers[args.mode].info("Dumping NTDS.DIT\t{}\t{}\\{}".format(target, args.domain, args.user))
    con.ntds()


def loggedon_users(con, args, target, loggers):
    x = RpcCon(args, loggers, target)
    x.get_netloggedon()
    for user, data in x.loggedon.items():
        if data['logon_srv']:
            loggers['console'].info([con.host, con.ip, "LOGGEDON", '{}\{:<25}'.format(data['domain'], user), "Logon_Server: {}".format(data['logon_srv'])])
        else:
            loggers['console'].info([con.host, con.ip, "LOGGEDON", '{}\{}'.format(data['domain'], user)])


def active_sessions(con, args, target, loggers):
    x = RpcCon(args, loggers, target)
    x.get_netsessions()
    for user, data in x.sessions.items():
        loggers['console'].info([con.host, con.ip, "SESSIONS", user, "Host: {}".format(data['host'])])


def tasklist(con, args, loggers):
    proc = WmiCon(args, loggers, con.ip, con.host)
    proc.get_netprocess(tasklist=True)


@requires_admin
def wmi_query(con, args, target, loggers):
    q = WmiCon(args, loggers, con.ip, con.host)
    loggers[args.mode].info("WMI Query\t{}\t{}\\{}\t{}".format(target, args.domain, args.user, args.wmi_query))
    q.wmi_query(args.wmi_namespace, args.wmi_query)

@requires_admin
def get_netlocalgroups(con, args, target, loggers):
    q = WmiCon(args, loggers, con.ip, con.host)
    loggers[args.mode].info("WMI Query\t{}\t{}\\{}\tEnumerate Local Groups".format(target, args.domain, args.user))
    q.get_netlocalgroups()

@requires_admin
def localgroup_members(smb_obj, args, target, loggers):
    q = WmiCon(args, loggers, smb_obj.ip, smb_obj.host)
    loggers[args.mode].info("WMI Query\t{}\t{}\\{}\tEnumerate Local Groups".format(target, args.domain, args.user))
    q.get_localgroup_members(smb_obj.con.getServerName(), args.local_members)

def execute_module(con, args, target, loggers, config_obj):
    if args.exec_method.lower() == "winrm" and args.module != "test_execution":
        loggers['console'].warning([con.host, con.ip, args.module.upper(), "WINRM Cannot be used for module execution outside of 'test_execution'"])
        return
    try:
        module_class = get_module_class(args.module)
        class_obj = module_class()
        # Admin check for module
        if class_obj.requires_admin and not con.admin:
            loggers['console'].warning([con.host, con.ip, args.module.upper(),"{} requires administrator access".format(args.module)])
            return

        populate_mod_args(class_obj, args.module_args, loggers['console'])
        loggers[args.mode].info("Module Execution\t{}\t{}\\{}\t{}".format(target, args.domain, args.user, args.module))
        class_obj.run(target, args, con, loggers, config_obj)
    except Exception as e:
        loggers['console'].fail([con.host, con.ip, args.module.upper(), "Error: {}".format(str(e))])


def host_enum(target, args, lockout, config_obj, db_obj, loggers):
    try:
        # OS Enumeration
        try:
            if args.exec_method == 'ssh':
                con = ssh_login(args, loggers, target, db_obj, lockout)
            else:
                con = smb_login(args, loggers, target, db_obj, lockout)
            if con.admin:
                loggers['console'].success([con.host, con.ip, "ENUM", con.os + con.os_arch, "(Domain: {})".format(con.srvdomain), "(Signing: {})".format(str(con.signing)), "(SMBv1: {})".format(str(con.smbv1)), "({})".format(highlight(config_obj.PWN3D_MSG, 'yellow'))])
            else:
                loggers['console'].info([con.host, con.ip, "ENUM", con.os + con.os_arch, "(Domain: {})".format(con.srvdomain),"(Signing: {})".format(str(con.signing)), "(SMBv1: {})".format(str(con.smbv1))])
        except Exception as e:
            return []

        shares = []
        if args.exec_method == 'ssh':
            if args.execute:
                # Override admin to allow execution
                con.admin = True
                code_execution(con, args, target, loggers, config_obj, args.execute)
        else:
            # Sharefinder
            if args.share:
                shares = args.share.split(",")
                for share in shares:
                    loggers['console'].info([con.host, con.ip, "SHAREFINDER", "\\\\{}\\{}".format(con.host, share)])

            elif args.sharefinder or args.spider:
                shares = share_finder(con, args, loggers, target)

            # Secondary actions
            if args.gen_relay_list and not con.signing:
                loggers['relay_list'].info(con.host)
            if args.passpol:
                password_policy(con, args, db_obj, loggers)
            if args.sam:
                extract_sam(con, args, target, loggers)
            if args.ntds:
                extract_ntds(con, args, target, loggers)
            if args.loggedon:
                loggedon_users(con, args, target, loggers)
            if args.sessions:
                active_sessions(con, args, target, loggers)
            if args.list_processes:
                tasklist(con, args, loggers)
            if args.local_groups:
                get_netlocalgroups(con, args, target, loggers)
            if args.local_members:
                localgroup_members(con, args, target, loggers)
            if args.wmi_query:
                wmi_query(con, args, target, loggers)
            if args.execute:
                code_execution(con, args, target, loggers, config_obj, args.execute)
            if args.ps_execute:
                ps_execution(con, args, target, loggers, config_obj)
            if args.module:
                execute_module(con, args, target, loggers, config_obj)

        # Close connections & return
        try:
            con.con.logoff()
        except:
            pass

        con.close()
        loggers['console'].debug("Shares returned for: {} {}".format(target, shares))
        return shares

    except KeyboardInterrupt:
        try:
            con.close()
        except:
            pass
        _exit(0)

    except Exception as e:
        loggers['console'].debug(str(e))